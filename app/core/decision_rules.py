"""Decision rules for the DEFENSE-side action recommendation engine (req #5/#11).

Chilean juicio ejecutivo. Every rule below is a DEFENSE action taken by the
ejecutado/deudor, and encodes a CONFIRMED-by-the-firm action (2026-07-09);
see ``memory/juicio-ejecutivo-defense-rules.md`` for the legal source
(CPC + Ley 21.394 + CC art. 2515 + DFL 707).

The firm's side is NOT fixed, despite what this docstring used to claim:
PJUD's litigante rows show 431 civil cases where a firm lawyer is the
ejecutante's abogado of record (AB.DTE/AP.DTE). Because these rules are
defense-only, ``DeadlineEngine._recompute_decision`` withholds the
recommendation whenever ``deadline_engine._firm_side`` CONFIRMS the firm is
"demandante". That helper answers "demandado" when it cannot tell, so the
~83% of cases without an abogado litigante keep the defense default.

These rules are a pure, declarative mapping over state ``DeadlineEngine``
has ALREADY computed (``DecisionContext``) — this module never touches the
DB and never recomputes procedural state itself. ``DecisionEngine``
(``app/services/decision_engine.py``) evaluates them.

All recommendations are advisory only — the lawyer decides. Every consumer
MUST surface that disclaimer to end-users (mirrors ``DEADLINE_DISCLAIMER``
in ``app/core/deadlines_config.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Callable, Optional

from app.core.deadlines_config import ProceduralState


# ---------------------------------------------------------------------------
# Legal disclaimer — MANDATORY wherever a recommended_action is surfaced.
# Distinct from DEADLINE_DISCLAIMER (which covers date *calculations*): this
# one covers the *recommendation itself* — it is system-generated advice,
# not a legal instruction.
# ---------------------------------------------------------------------------
RECOMMENDATION_DISCLAIMER: str = (
    "Esta es una sugerencia del sistema; la decisión final es del abogado."
)


# ---------------------------------------------------------------------------
# Urgency
# ---------------------------------------------------------------------------
class Urgency(str, Enum):
    """Recommendation urgency tiers."""

    CRITICA = "critica"
    ALTA = "alta"
    MEDIA = "media"
    BAJA = "baja"


# ---------------------------------------------------------------------------
# DecisionContext — the input every rule predicate reasons over
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DecisionContext:
    """Snapshot of a case's ALREADY-COMPUTED procedural state.

    Built by ``DecisionEngine`` from the ``Case`` columns
    ``DeadlineEngine.recompute_case`` writes (Step 8 of its pipeline) — this
    dataclass never recomputes anything, it only carries values. Kept
    separate from the ORM ``Case`` model so rules can be unit tested with
    zero DB dependency (see ``tests/unit/test_decision_engine.py``).
    """

    procedural_state: Optional[str]
    semaforo: Optional[str]
    abandono_disponible: bool
    prescripcion_cumplida: bool
    en_apremio: bool
    next_deadline_at: Optional[date]
    next_deadline_fatal: bool
    today: date


# ---------------------------------------------------------------------------
# DecisionRule
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DecisionRule:
    """One prioritized DEFENSE action rule.

    ``predicate`` receives a ``DecisionContext`` and returns True when the
    rule fires. Rules are evaluated in ``DECISION_RULES`` list order — the
    FIRST match wins, so list order IS priority order.
    """

    code: str
    action_text: str
    legal_basis: str
    urgency: Urgency
    predicate: Callable[[DecisionContext], bool]


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------
def _has_open_fatal_window(ctx: DecisionContext) -> bool:
    """True when the firm's nearest actionable deadline is FATAL and its
    window is still open (due_date is today or later — not yet missed)."""
    return (
        ctx.next_deadline_fatal
        and ctx.next_deadline_at is not None
        and ctx.next_deadline_at >= ctx.today
    )


def _is_excepciones_window(ctx: DecisionContext) -> bool:
    """Requerido de pago, within the 8-día-hábil opposition window (art.
    459/464 CPC). Miss it → mandamiento hace las veces de sentencia (472)."""
    return (
        ctx.procedural_state == ProceduralState.NOTIFICADO.value
        and _has_open_fatal_window(ctx)
    )


def _is_apelacion_window(ctx: DecisionContext) -> bool:
    """Sentencia condenatoria notified, within the 5-día apelación window
    (art. 475 CPC — ejecutado: solo efecto devolutivo)."""
    return (
        ctx.procedural_state == ProceduralState.SENTENCIA.value
        and _has_open_fatal_window(ctx)
    )


def _is_prescripcion(ctx: DecisionContext) -> bool:
    return ctx.prescripcion_cumplida


def _is_abandono(ctx: DecisionContext) -> bool:
    return ctx.abandono_disponible


def _is_auto_prueba(ctx: DecisionContext) -> bool:
    return ctx.procedural_state == ProceduralState.AUTO_PRUEBA.value


def _is_apremio(ctx: DecisionContext) -> bool:
    return ctx.en_apremio


def _is_generic_alert(ctx: DecisionContext) -> bool:
    """Catch-all: semáforo rojo with a fatal deadline pending/missed, but
    none of the specific rules above matched. Should rarely fire — it's a
    defensive fallback so a rojo case is never silently unrecommended."""
    return ctx.semaforo == "rojo" and ctx.next_deadline_fatal


# ---------------------------------------------------------------------------
# DECISION_RULES — priority order (first match wins).
#
# Priority tiers (highest → lowest), per the firm's confirmed rules
# (memory/juicio-ejecutivo-defense-rules.md):
#   1. Time-critical FATAL deadline still open (excepciones / apelación) —
#      miss it and the defense loses a right outright.
#   2. Standing opportunities (prescripción / abandono) — available any
#      time once triggered, but should be exercised promptly.
#   3. Procedural step (auto de prueba).
#   4. Apremio — post-judgment execution phase; urgent but not a
#      rights-ending window like tier 1.
#   5. Generic rojo+fatal catch-all.
# ---------------------------------------------------------------------------
DECISION_RULES: list[DecisionRule] = [
    DecisionRule(
        code="oponer_excepciones",
        action_text="Oponer excepciones (escrito de oposición)",
        legal_basis="art. 459/464 CPC",
        urgency=Urgency.CRITICA,
        predicate=_is_excepciones_window,
    ),
    DecisionRule(
        code="evaluar_apelacion",
        action_text="Evaluar apelación (5 días, solo efecto devolutivo)",
        legal_basis="art. 475 CPC",
        urgency=Urgency.CRITICA,
        predicate=_is_apelacion_window,
    ),
    DecisionRule(
        code="oponer_prescripcion",
        action_text="Oponer prescripción de la acción",
        legal_basis="art. 98 DFL 707 / 2515 CC",
        urgency=Urgency.ALTA,
        predicate=_is_prescripcion,
    ),
    DecisionRule(
        code="solicitar_abandono",
        action_text="Solicitar abandono del procedimiento",
        legal_basis="art. 152 / 153 inc. 2 CPC",
        urgency=Urgency.ALTA,
        predicate=_is_abandono,
    ),
    DecisionRule(
        code="reposicion_prueba",
        action_text="Reposición + ofrecer prueba (lista de testigos primeros 2 días)",
        legal_basis="art. 318-319 CPC",
        urgency=Urgency.MEDIA,
        predicate=_is_auto_prueba,
    ),
    DecisionRule(
        code="gestion_apremio",
        action_text="Gestión urgente — evaluar tercerías / objeción de tasación",
        legal_basis="arts. 486-518 CPC",
        urgency=Urgency.CRITICA,
        predicate=_is_apremio,
    ),
    DecisionRule(
        code="atencion_inmediata",
        action_text="Atención inmediata — plazo fatal próximo a vencer o vencido",
        legal_basis="",
        urgency=Urgency.CRITICA,
        predicate=_is_generic_alert,
    ),
]


_RULES_BY_CODE: dict[str, DecisionRule] = {rule.code: rule for rule in DECISION_RULES}


def resolve_rule(code: Optional[str]) -> Optional[DecisionRule]:
    """Look up a ``DecisionRule`` by its ``code`` — used at the API layer to
    resolve ``Case.recommended_action_code`` back into display text."""
    if code is None:
        return None
    return _RULES_BY_CODE.get(code)
