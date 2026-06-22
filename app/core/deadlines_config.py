"""Procedural deadline configuration for juicio ejecutivo (CPC, Chile).

All legal interpretations are advisory.  Every consumer of this module
MUST surface DEADLINE_DISCLAIMER to end-users.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Legal disclaimer — MANDATORY in every API response that exposes deadlines.
# ---------------------------------------------------------------------------
DEADLINE_DISCLAIMER: str = (
    "Este cálculo es orientativo y no reemplaza el criterio del abogado."
)


# ---------------------------------------------------------------------------
# ProceduralState — juicio ejecutivo states (CPC + Ley 21.394)
# ---------------------------------------------------------------------------
class ProceduralState(str, Enum):
    """Procedural states for a juicio ejecutivo civil."""

    MANDAMIENTO = "mandamiento"
    NOTIFICADO = "notificado"
    EXCEPCIONES = "excepciones"
    TRASLADO_EJECUTANTE = "traslado_ejecutante"
    ADMISIBILIDAD = "admisibilidad"
    AUTO_PRUEBA = "auto_prueba"
    CITACION_SENTENCIA = "citacion_sentencia"
    SENTENCIA = "sentencia"
    TERMINADA = "terminada"
    REBELDE = "rebelde"
    INDETERMINATE = "indeterminate"


# ---------------------------------------------------------------------------
# ProcEvent — events that drive state transitions
# ---------------------------------------------------------------------------
class ProcEvent(str, Enum):
    """Triggering events mapped to classifier rules."""

    MANDAMIENTO = "mandamiento"
    NOTIFICACION_EXITOSA = "notificacion_exitosa"
    EXCEPCIONES_OPUESTAS = "excepciones_opuestas"
    TRASLADO_EJECUTANTE = "traslado_ejecutante"
    ADMISIBILIDAD = "admisibilidad"
    AUTO_PRUEBA = "auto_prueba"
    CITACION_SENTENCIA = "citacion_sentencia"
    SENTENCIA = "sentencia"
    TERMINADA = "terminada"


# ---------------------------------------------------------------------------
# DeadlineType — legal plazos with CPC article and días hábiles count
# ---------------------------------------------------------------------------
class DeadlineType(str, Enum):
    """Legal deadline types.  Each member carries ``dias_habiles`` and
    ``legal_basis`` as extra attributes for display / computation.
    """

    def __new__(
        cls,
        value: str,
        dias_habiles: int = 0,
        legal_basis: str = "",
        is_fatal: bool = False,
    ) -> "DeadlineType":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.dias_habiles = dias_habiles  # type: ignore[attr-defined]
        obj.legal_basis = legal_basis  # type: ignore[attr-defined]
        obj.is_fatal = is_fatal  # type: ignore[attr-defined]
        return obj

    EXCEPCIONES_8D = ("excepciones_8d", 8, "art. 459 CPC", True)
    TRASLADO_EJECUTANTE_4D = ("traslado_ejecutante_4d", 4, "art. 466 CPC")
    TERMINO_PROBATORIO_10D = ("termino_probatorio_10d", 10, "art. 468 CPC")
    # Stub — secondary deadline deferred to Slice B
    LISTA_TESTIGOS_2D = ("lista_testigos_2d", 2, "art. 468 CPC")
    OBSERVACIONES_PRUEBA_6D = ("observaciones_prueba_6d", 6, "art. 469 CPC")
    SENTENCIA_10D = ("sentencia_10d", 10, "art. 162/470 CPC")
    APELACION_5D = ("apelacion_5d", 5, "art. 187/475 CPC", True)


# ---------------------------------------------------------------------------
# Deadline ownership — from the EJECUTANTE's (creditor's) perspective.
#
# The semáforo reflects ONLY the firm's own actionable deadlines. Counterparty
# (debtor) and court deadlines are informational and must NEVER by themselves
# produce ROJO.
#   MANDATORY_ACTIONABLE — the firm MUST act; if the window EXPIRES → ROJO.
#   OPTIONAL_ACTIONABLE  — the firm MAY act; drives the color while active, but
#                          once EXPIRED it is just a closed window (NOT red).
#   INFORMATIONAL        — debtor's / court's window; never drives the color.
#     · EXCEPCIONES_8D — debtor's window to oppose. Lapsing without opposition
#       = REBELDÍA = favourable for the creditor (proceed to sentencia).
#     · SENTENCIA_10D  — court's term to rule; the firm only waits.
# ---------------------------------------------------------------------------
MANDATORY_ACTIONABLE: frozenset[DeadlineType] = frozenset({
    DeadlineType.TRASLADO_EJECUTANTE_4D,
    DeadlineType.TERMINO_PROBATORIO_10D,
    DeadlineType.LISTA_TESTIGOS_2D,
    DeadlineType.OBSERVACIONES_PRUEBA_6D,
})
OPTIONAL_ACTIONABLE: frozenset[DeadlineType] = frozenset({
    DeadlineType.APELACION_5D,
})
ACTIONABLE_DEADLINES: frozenset[DeadlineType] = MANDATORY_ACTIONABLE | OPTIONAL_ACTIONABLE
INFORMATIONAL_DEADLINES: frozenset[DeadlineType] = frozenset({
    DeadlineType.EXCEPCIONES_8D,
    DeadlineType.SENTENCIA_10D,
})

# String-value views (CaseDeadline.deadline_type stores the .value string).
MANDATORY_ACTIONABLE_VALUES: frozenset[str] = frozenset(d.value for d in MANDATORY_ACTIONABLE)
OPTIONAL_ACTIONABLE_VALUES: frozenset[str] = frozenset(d.value for d in OPTIONAL_ACTIONABLE)
ACTIONABLE_DEADLINE_VALUES: frozenset[str] = frozenset(d.value for d in ACTIONABLE_DEADLINES)


def actionable_sets(side: str) -> "tuple[frozenset[str], frozenset[str]]":
    """Return (mandatory_values, actionable_values) for the firm's side.

    Chilean juicio-ejecutivo logic:
    - DEMANDADO (debtor, ~73%): must oppose with EXCEPCIONES_8D — that is THE
      mandatory deadline the firm must not miss.
    - DEMANDANTE (creditor): replies with TRASLADO_EJECUTANTE_4D after the
      court grants traslado following the debtor's excepciones.
    - APELACION_5D stays optional for both sides (it is the firm's right, not
      an obligation, to appeal the sentencia).

    Returns:
        (mandatory_values, actionable_values) — both as frozenset[str] of
        DeadlineType.value strings, suitable for SQL ``in_()`` filters.
    """
    base_mandatory = {
        DeadlineType.TERMINO_PROBATORIO_10D,
        DeadlineType.OBSERVACIONES_PRUEBA_6D,
        DeadlineType.LISTA_TESTIGOS_2D,
    }
    if side == "demandante":
        mandatory = base_mandatory | {DeadlineType.TRASLADO_EJECUTANTE_4D}
    else:  # demandado (default — the firm primarily defends debtors)
        mandatory = base_mandatory | {DeadlineType.EXCEPCIONES_8D}
    optional = {DeadlineType.APELACION_5D}
    mandatory_v = frozenset(d.value for d in mandatory)
    actionable_v = frozenset(d.value for d in (mandatory | optional))
    return mandatory_v, actionable_v


# ---------------------------------------------------------------------------
# ClassifierRule — config-driven rule for the movement classifier
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClassifierRule:
    """A single rule in the procedural classifier.

    Both ``description_regex`` and ``stage_regex`` may be non-empty; when
    both are set BOTH must match.  Use an empty string or ``None`` to skip
    a field check.

    ``min_state``: when set, the rule only fires when the case's current
    state is at or beyond the given state in the forward-only ordering.
    This prevents early-stage rules from firing in medida-prejudicial context.

    Priority: higher number wins a tie on the same movement.
    """

    description_regex: str
    stage_regex: str
    event: ProcEvent
    next_state: ProceduralState
    starts_deadline_type: Optional[DeadlineType]
    priority: int
    min_state: Optional[ProceduralState] = None

    # Compiled regex objects (not part of the frozen hash)
    _desc_re: re.Pattern = field(init=False, repr=False, compare=False, hash=False)
    _stage_re: re.Pattern = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        # Store on instance bypassing frozen restriction
        object.__setattr__(
            self,
            "_desc_re",
            re.compile(self.description_regex) if self.description_regex else None,
        )
        object.__setattr__(
            self,
            "_stage_re",
            re.compile(self.stage_regex) if self.stage_regex else None,
        )

    def matches(self, description: str, stage: str) -> bool:
        """Return True when this rule applies to a movement (text only)."""
        if self._desc_re is not None and not self._desc_re.search(description):
            return False
        if self._stage_re is not None and not self._stage_re.search(stage):
            return False
        return True


# ---------------------------------------------------------------------------
# CLASSIFIER_RULES — ordered list; each movement is matched against all rules
# and the highest-priority matching rule wins.
# Grounded in real movement data from scripts/manual/real_movements.json.
# ---------------------------------------------------------------------------
CLASSIFIER_RULES: list[ClassifierRule] = [
    # Rule 1 — Mandamiento despachado
    ClassifierRule(
        description_regex=r"[Oo]rdena despachar mandamiento",
        stage_regex="",
        event=ProcEvent.MANDAMIENTO,
        next_state=ProceduralState.MANDAMIENTO,
        starts_deadline_type=None,
        priority=3,
    ),
    # Rule 2 — Notificación exitosa de la demanda → NOTIFICADO + EXCEPCIONES_8D
    # Real data: "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:..."
    # Must NOT match "(Certificación)" attempts.
    ClassifierRule(
        description_regex=(
            r"(NOTIFICACI[ÓO]N DE DEMANDA|Notificaci[oó]n [Dd]emanda)"
            r".*[Ee]xitosa"
        ),
        stage_regex="",
        event=ProcEvent.NOTIFICACION_EXITOSA,
        next_state=ProceduralState.NOTIFICADO,
        starts_deadline_type=DeadlineType.EXCEPCIONES_8D,
        priority=6,
    ),
    # Rule 3 — Excepciones stage → EXCEPCIONES (no deadline yet)
    # Real data: stage == "Excepciones"
    # NOTE: TRASLADO_EJECUTANTE_4D is intentionally NOT triggered here.
    # Legally the 4-day traslado runs from the court's traslado resolution,
    # not from the raw excepciones filing.  Triggering from the filing would
    # be 1-3 days too early and could show a false ROJO before the legal
    # deadline starts.  The trigger is deferred to Rule 4 (more conservative /
    # later date).  See also: PR1 review fix #5.
    # Real stages where the debtor opposes: "Excepciones" and "Oposición de
    # Excepciones" (both seen in real PJUD data). Both → EXCEPCIONES state.
    ClassifierRule(
        description_regex="",
        stage_regex=r"^Excepciones$|^Oposici[oó]n de Excepciones$",
        event=ProcEvent.EXCEPCIONES_OPUESTAS,
        next_state=ProceduralState.EXCEPCIONES,
        starts_deadline_type=None,
        priority=4,
    ),
    # Rule 4 — Contestación Excepciones stage → TRASLADO_EJECUTANTE + TRASLADO_EJECUTANTE_4D
    # Real data: stage == "Contestación Excepciones"
    # The 4-day traslado is triggered from the court's traslado resolution
    # (the first "Contestación Excepciones" stage movement), which is the legally
    # correct and more conservative (later) trigger date.
    ClassifierRule(
        description_regex="",
        stage_regex=r"Contestaci[oó]n Excepciones",
        event=ProcEvent.TRASLADO_EJECUTANTE,
        next_state=ProceduralState.TRASLADO_EJECUTANTE,
        starts_deadline_type=DeadlineType.TRASLADO_EJECUTANTE_4D,
        priority=5,
    ),
    # Rule 5 — Se pronuncia sobre admisibilidad → ADMISIBILIDAD
    # Real data: desc "Se pronuncia sobre admisibilidad excep."
    ClassifierRule(
        description_regex=r"[Ss]e pronuncia sobre admisibilidad",
        stage_regex="",
        event=ProcEvent.ADMISIBILIDAD,
        next_state=ProceduralState.ADMISIBILIDAD,
        starts_deadline_type=None,
        priority=7,
    ),
    # Rule 6 — Notificación resolución que recibe a prueba → AUTO_PRUEBA
    # Real data (truncated): "Notificación resolución que recibe la causa a prue (Exitosa)..."
    # Regex uses "prue" prefix to match both "prueba" and truncated "prue".
    ClassifierRule(
        description_regex=r"Notificaci[oó]n resoluci[oó]n que recibe.*prue",
        stage_regex="",
        event=ProcEvent.AUTO_PRUEBA,
        next_state=ProceduralState.AUTO_PRUEBA,
        starts_deadline_type=DeadlineType.TERMINO_PROBATORIO_10D,
        priority=9,
    ),
    # Rule 7 — Citación a oír sentencia → CITACION_SENTENCIA + SENTENCIA_10D
    # Real data: "Cita a Audiencia", "Citación a oír sentencia" (20 occurrences),
    #            "Citación para oír sentencia" (17 occurrences).
    # min_state=NOTIFICADO: prevents this rule from firing in a medida-prejudicial
    # context where a hearing is scheduled before the demand has been notified
    # (which would produce a false SENTENCIA_10D deadline).
    ClassifierRule(
        description_regex=r"Cita a Audiencia|Citaci[óo]n (a|para) o[íi]r sentencia",
        stage_regex="",
        event=ProcEvent.CITACION_SENTENCIA,
        next_state=ProceduralState.CITACION_SENTENCIA,
        starts_deadline_type=DeadlineType.SENTENCIA_10D,
        priority=8,
        min_state=ProceduralState.NOTIFICADO,
    ),
    # Rule 8 — Terminada stage → TERMINADA (terminal state)
    ClassifierRule(
        description_regex="",
        stage_regex=r"^Terminada$",
        event=ProcEvent.TERMINADA,
        next_state=ProceduralState.TERMINADA,
        starts_deadline_type=None,
        priority=10,
    ),
    # Rule 9 — Sentencia definitiva → SENTENCIA + APELACION_5D
    # Real data: "Dicta sentencia", "Sentencia Definitiva", "Sentencia" (exact),
    #            "Notificación de la sentencia".
    # The 5-day apelación plazo runs from the date the sentencia is issued
    # (art. 187 CPC, applied by art. 475 for juicio ejecutivo).
    # min_state=CITACION_SENTENCIA: sentencia can only be issued after
    # the citación stage has been reached.
    ClassifierRule(
        description_regex=r"[Dd]icta [Ss]entencia|[Ss]entencia [Dd]efinitiva|^Sentencia$|Notificaci[óo]n de la sentencia",
        stage_regex="",
        event=ProcEvent.SENTENCIA,
        next_state=ProceduralState.SENTENCIA,
        starts_deadline_type=DeadlineType.APELACION_5D,
        priority=11,
        min_state=ProceduralState.CITACION_SENTENCIA,
    ),
]
