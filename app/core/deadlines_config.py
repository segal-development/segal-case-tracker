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
    ) -> "DeadlineType":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.dias_habiles = dias_habiles  # type: ignore[attr-defined]
        obj.legal_basis = legal_basis  # type: ignore[attr-defined]
        return obj

    EXCEPCIONES_8D = ("excepciones_8d", 8, "art. 459 CPC")
    TRASLADO_EJECUTANTE_4D = ("traslado_ejecutante_4d", 4, "art. 466 CPC")
    TERMINO_PROBATORIO_10D = ("termino_probatorio_10d", 10, "art. 468 CPC")
    # Stub — secondary deadline deferred to Slice B
    LISTA_TESTIGOS_2D = ("lista_testigos_2d", 2, "art. 468 CPC")
    OBSERVACIONES_PRUEBA_6D = ("observaciones_prueba_6d", 6, "art. 469 CPC")
    SENTENCIA_10D = ("sentencia_10d", 10, "art. 162/470 CPC")
    APELACION_5D = ("apelacion_5d", 5, "art. 189/475 CPC")


# ---------------------------------------------------------------------------
# ClassifierRule — config-driven rule for the movement classifier
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClassifierRule:
    """A single rule in the procedural classifier.

    Both ``description_regex`` and ``stage_regex`` may be non-empty; when
    both are set BOTH must match.  Use an empty string or ``None`` to skip
    a field check.

    Priority: higher number wins a tie on the same movement.
    """

    description_regex: str
    stage_regex: str
    event: ProcEvent
    next_state: ProceduralState
    starts_deadline_type: Optional[DeadlineType]
    priority: int

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
        """Return True when this rule applies to a movement."""
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
    # Rule 3 — Excepciones stage → EXCEPCIONES + TRASLADO_EJECUTANTE_4D
    # Real data: stage == "Excepciones"
    ClassifierRule(
        description_regex="",
        stage_regex=r"^Excepciones$",
        event=ProcEvent.EXCEPCIONES_OPUESTAS,
        next_state=ProceduralState.EXCEPCIONES,
        starts_deadline_type=DeadlineType.TRASLADO_EJECUTANTE_4D,
        priority=4,
    ),
    # Rule 4 — Contestación Excepciones stage → TRASLADO_EJECUTANTE (no new deadline)
    # Real data: stage == "Contestación Excepciones"
    ClassifierRule(
        description_regex="",
        stage_regex=r"Contestaci[oó]n Excepciones",
        event=ProcEvent.TRASLADO_EJECUTANTE,
        next_state=ProceduralState.TRASLADO_EJECUTANTE,
        starts_deadline_type=None,
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
    # Rule 7 — Cita a Audiencia → CITACION_SENTENCIA + SENTENCIA_10D
    # Real data: desc "Cita a Audiencia"
    ClassifierRule(
        description_regex=r"Cita a Audiencia",
        stage_regex="",
        event=ProcEvent.CITACION_SENTENCIA,
        next_state=ProceduralState.CITACION_SENTENCIA,
        starts_deadline_type=DeadlineType.SENTENCIA_10D,
        priority=8,
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
]
