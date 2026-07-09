"""Procedural classifier for civil juicio ejecutivo (CPC, Chile).

Walks a case's movements chronologically and applies a priority-ordered
rule list to derive the current ProceduralState and the triggering
movement (or computed date) for each active deadline.

ADVISORY: all output is advisory.  Any exception in classification
is caught and returns (INDETERMINATE, {}) so a bad rule can never
crash the sync pipeline.

Key design points:
- One pass, oldest-first (sorted by movement_date).
- Per movement: all rules are evaluated; highest-priority match wins.
- ties (same priority) are broken by rule order in CLASSIFIER_RULES.
- OBSERVACIONES_PRUEBA_6D trigger is computed from TERMINO_PROBATORIO_10D
  due date (a derived date, not a scraped movement).
- LISTA_TESTIGOS_2D and REPOSICION_AUTO_PRUEBA_3D (Slice 2b) are derived from
  the SAME AUTO_PRUEBA trigger movement as TERMINO_PROBATORIO_10D — they run
  in parallel with the término probatorio, not after it like OBSERVACIONES.
- REBELDÍA is NOT decided here; the deadline engine checks it after classification.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Union

from app.core.deadlines_config import (
    CLASSIFIER_RULES,
    ClassifierRule,
    DeadlineType,
    ProceduralState,
)
from app.services.business_days import add_business_days

logger = logging.getLogger(__name__)

# Type alias — a deadline trigger is either a movement (has movement_date)
# or a pre-computed date (for derived deadlines like OBSERVACIONES).
Trigger = Union[object, date]  # object = any Movement-like duck-typed instance

# ---------------------------------------------------------------------------
# Forward-only state ordering
# Real PJUD data shows that once a later state is reached (e.g. AUTO_PRUEBA),
# earlier-stage movements can still appear (e.g. "Contestación Excepciones"
# stage after AUTO_PRUEBA entry).  We must never regress to a lower state.
# INDETERMINATE (0) is the starting sentinel; TERMINADA (8) is terminal.
# REBELDE is special (engine-computed); the classifier never sets it.
# ---------------------------------------------------------------------------
_STATE_ORDER: dict[ProceduralState, int] = {
    ProceduralState.INDETERMINATE: 0,
    ProceduralState.MANDAMIENTO: 1,
    ProceduralState.NOTIFICADO: 2,
    ProceduralState.EXCEPCIONES: 3,
    ProceduralState.TRASLADO_EJECUTANTE: 4,
    ProceduralState.ADMISIBILIDAD: 5,
    ProceduralState.AUTO_PRUEBA: 6,
    ProceduralState.CITACION_SENTENCIA: 7,
    ProceduralState.SENTENCIA: 8,
    ProceduralState.TERMINADA: 9,
    # REBELDE is handled by the engine, not the classifier
    ProceduralState.REBELDE: 10,
}


class MovementClassifier:
    """Config-driven procedural state classifier.

    Accepts any objects with ``.stage``, ``.description``, and
    ``.movement_date`` attributes (works with both SQLAlchemy ``Movement``
    rows and the ``FakeMovement`` stubs used in unit tests).
    """

    def classify(
        self,
        movements: list,
        today: date,
    ) -> tuple[ProceduralState, dict[DeadlineType, Trigger]]:
        """Classify *movements* and return (state, triggers).

        Safe-fails to (INDETERMINATE, {}) on any exception so callers
        never need to guard against classifier errors.

        Args:
            movements: List of movement-like objects (Movement or FakeMovement).
                       Each must expose .stage, .description, .movement_date.
            today:     Reference date (used to derive OBSERVACIONES computed trigger).

        Returns:
            Tuple of (ProceduralState, triggers_dict).
            triggers_dict maps DeadlineType → triggering Movement | date.
        """
        try:
            return self._classify(movements, today)
        except Exception as exc:
            logger.exception(
                "MovementClassifier.classify failed — returning INDETERMINATE: %s",
                exc,
            )
            return ProceduralState.INDETERMINATE, {}

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    def _classify(
        self,
        movements: list,
        today: date,
    ) -> tuple[ProceduralState, dict[DeadlineType, Trigger]]:
        if not movements:
            return ProceduralState.INDETERMINATE, {}

        # Sort chronologically (oldest first) to walk state transitions in order.
        sorted_movements = sorted(movements, key=lambda m: m.movement_date)

        state: ProceduralState = ProceduralState.INDETERMINATE
        triggers: dict[DeadlineType, Trigger] = {}
        any_match = False

        for movement in sorted_movements:
            desc = (movement.description or "") if movement.description is not None else ""
            stage = (movement.stage or "") if movement.stage is not None else ""

            best_rule = self._best_matching_rule(desc, stage, state)

            if best_rule is None:
                continue

            any_match = True

            # Only advance state forward — never regress.
            # TERMINADA/SENTENCIA can always be entered from their required min_state.
            candidate = best_rule.next_state
            if _STATE_ORDER.get(candidate, 0) <= _STATE_ORDER.get(state, 0):
                # Regression attempt — skip entirely.
                continue

            # State advances: clear accumulated triggers from prior state.
            # This ensures only the CURRENT state's deadlines are active.
            # Example: EXCEPCIONES_8D is cleared when advancing to EXCEPCIONES,
            # so a stale past-due EXCEPCIONES_8D never produces a false ROJO.
            triggers = {}
            state = best_rule.next_state

            if best_rule.starts_deadline_type is not None:
                deadline_type = best_rule.starts_deadline_type
                triggers[deadline_type] = movement

                # OBSERVACIONES_PRUEBA_6D is derived from TERMINO_PROBATORIO_10D.
                if deadline_type == DeadlineType.TERMINO_PROBATORIO_10D:
                    trigger_date = self._movement_date(movement)
                    tp_due = add_business_days(trigger_date, DeadlineType.TERMINO_PROBATORIO_10D.dias_habiles)
                    triggers[DeadlineType.OBSERVACIONES_PRUEBA_6D] = tp_due
                    # LISTA_TESTIGOS_2D (art. 320 CPC) and REPOSICION_AUTO_PRUEBA_3D
                    # (arts. 318-319 CPC) run from the SAME auto de prueba
                    # notification as TERMINO_PROBATORIO_10D — not from its due
                    # date like OBSERVACIONES — so they reuse the movement itself.
                    triggers[DeadlineType.LISTA_TESTIGOS_2D] = movement
                    triggers[DeadlineType.REPOSICION_AUTO_PRUEBA_3D] = movement

        if not any_match:
            return ProceduralState.INDETERMINATE, {}

        return state, triggers

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _best_matching_rule(
        self, description: str, stage: str, current_state: ProceduralState
    ) -> ClassifierRule | None:
        """Return the highest-priority rule that matches (desc, stage, state), or None.

        Rules with a ``min_state`` are skipped when the current state has not
        yet reached that minimum.  This prevents early-stage rules (e.g.
        Rule 7 "Cita a Audiencia") from firing in medida-prejudicial context.
        """
        best: ClassifierRule | None = None
        for rule in CLASSIFIER_RULES:
            if rule.matches(description, stage):
                if rule.min_state is not None:
                    if _STATE_ORDER.get(current_state, 0) < _STATE_ORDER.get(rule.min_state, 0):
                        continue
                if best is None or rule.priority > best.priority:
                    best = rule
        return best

    @staticmethod
    def _movement_date(movement: object) -> date:
        """Extract a ``date`` from a movement-like object's ``movement_date``."""
        mv_dt = movement.movement_date  # type: ignore[union-attr]
        if hasattr(mv_dt, "date"):
            return mv_dt.date()
        return mv_dt  # already a date
