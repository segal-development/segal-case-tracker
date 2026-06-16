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
- LISTA_TESTIGOS_2D is intentionally excluded from computation (Slice B).
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
    ProceduralState.TERMINADA: 8,
    # REBELDE is handled by the engine, not the classifier
    ProceduralState.REBELDE: 9,
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

            best_rule = self._best_matching_rule(desc, stage)

            if best_rule is None:
                continue

            any_match = True

            # Only advance state forward — never regress.
            # TERMINADA can always be entered (terminal state from any point).
            candidate = best_rule.next_state
            if (
                candidate != ProceduralState.TERMINADA
                and _STATE_ORDER.get(candidate, 0) <= _STATE_ORDER.get(state, 0)
            ):
                # Regression attempt: rule matches but doesn't advance state.
                # Still record deadline triggers if the deadline is new.
                if best_rule.starts_deadline_type is not None:
                    pass  # handled below; deadline assignment is still skipped
                continue

            state = best_rule.next_state

            if best_rule.starts_deadline_type is not None:
                deadline_type = best_rule.starts_deadline_type

                # LISTA_TESTIGOS_2D deferred to Slice B — skip computation.
                if deadline_type == DeadlineType.LISTA_TESTIGOS_2D:
                    continue

                triggers[deadline_type] = movement

                # OBSERVACIONES_PRUEBA_6D is derived from TERMINO_PROBATORIO_10D.
                if deadline_type == DeadlineType.TERMINO_PROBATORIO_10D:
                    trigger_date = self._movement_date(movement)
                    tp_due = add_business_days(trigger_date, DeadlineType.TERMINO_PROBATORIO_10D.dias_habiles)
                    triggers[DeadlineType.OBSERVACIONES_PRUEBA_6D] = tp_due

        if not any_match:
            return ProceduralState.INDETERMINATE, {}

        return state, triggers

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _best_matching_rule(
        self, description: str, stage: str
    ) -> ClassifierRule | None:
        """Return the highest-priority rule that matches (desc, stage), or None."""
        best: ClassifierRule | None = None
        for rule in CLASSIFIER_RULES:
            if rule.matches(description, stage):
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
