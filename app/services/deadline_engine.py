"""Deadline engine for civil juicio ejecutivo procedural deadlines.

Implements the 8-step recompute pipeline:
  1. Load case movements ordered ASC by movement_date.
  2. Guard: non-civil competencia → GRIS + return.
  3. Classify → (ProceduralState, triggers dict).
  4. UPSERT case_deadlines on (case_id, deadline_type, triggered_at).
  5. Mark superseded: any active row for a deadline type whose triggered_at
     differs from the new trigger is superseded.
  6. REBELDÍA post-transition: NOTIFICADO + 8 días hábiles elapsed + no
     EXCEPCIONES movement → flip to REBELDE, mark EXCEPCIONES_8D expired.
  7. Compute semáforo from nearest active deadline + abandono/prescripción flags.
  8. Write Case.procedural_state, Case.semaforo, Case.next_deadline_at + flush.

NEVER raises. Any exception → safe-fail to GRIS, log the error.
ALWAYS surfaces DEADLINE_DISCLAIMER in every API response (enforced at API layer).

TODO: Slice B — differentiate pagaré (1y prescripción, art. 98 Ley 18.092) via
      Case.instrument_type once that column is added.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.deadlines_config import (
    DEADLINE_DISCLAIMER,
    DeadlineType,
    ProceduralState,
)
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.movement import Movement
from app.services.business_days import add_business_days, count_business_days_remaining
from app.services.procedural_classifier import MovementClassifier

logger = logging.getLogger(__name__)

_CLASSIFIER = MovementClassifier()

# Semáforo thresholds (in días hábiles)
_SEMAFORO_ROJO_MAX = 1    # ≤ 1 → ROJO (0 or negative also ROJO)
_SEMAFORO_AMARILLO_MAX = 5  # 2–5 → AMARILLO; >5 → VERDE

# Abandono risk thresholds (in days)
_ABANDONO_APPROACHING_DAYS = 135  # 4.5 months
_ABANDONO_PRESUMIBLE_DAYS = 180   # 6 months (art. 152 CPC)
_ABANDONO_REBELDE_APPROACHING_DAYS = 912  # ~2.5 years
_ABANDONO_REBELDE_PRESUMIBLE_DAYS = 1095  # ~3 years (art. 153 inc. 2 CPC)

# Prescripción risk thresholds (in days, art. 2515 CC — acción ejecutiva 3y)
_PRESCRIPCION_APPROACHING_DAYS = 912  # ~2.5 years
_PRESCRIPCION_AT_RISK_DAYS = 1095    # ~3 years


class DeadlineEngine:
    """Stateless engine — all state lives in the DB row and is recomputed on sync."""

    @classmethod
    def recompute_case(cls, db: Session, case: Case) -> None:
        """Recompute procedural deadlines for *case* and flush to DB.

        Flush-only: the outer caller is responsible for commit/rollback.
        Never raises — any exception is logged and GRIS is written to the Case.
        Only processes civil competencia; all others silently become GRIS.

        Args:
            db:   Active SQLAlchemy session (shared with the sync transaction).
            case: Case ORM instance to update (modified in-place + flushed).
        """
        try:
            cls._recompute_safe(db, case)
        except Exception as exc:
            logger.exception(
                "DeadlineEngine.recompute_case failed for case_id=%s: %s — "
                "falling back to GRIS",
                getattr(case, "id", "?"),
                exc,
            )
            cls._write_gris(case, db)

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    @classmethod
    def _recompute_safe(cls, db: Session, case: Case) -> None:
        today = date.today()

        # Step 2: competencia guard.
        competencia = (case.competencia or "civil").lower()
        if competencia != "civil":
            cls._write_gris(case, db)
            return

        # Step 1: load movements ASC.
        movements: list[Movement] = (
            db.query(Movement)
            .filter(Movement.case_id == case.id)
            .order_by(Movement.movement_date.asc())
            .all()
        )

        # Step 3: classify.
        proc_state, triggers = _CLASSIFIER.classify(movements, today)

        # Step 4: upsert case_deadlines for each active trigger.
        new_trigger_keys: dict[str, date] = {}  # deadline_type → triggered_at

        for deadline_type, trigger in triggers.items():
            triggered_at = cls._triggered_at_date(trigger)
            due_date = add_business_days(triggered_at, deadline_type.dias_habiles)

            new_trigger_keys[deadline_type.value] = triggered_at

            # Upsert on (case_id, deadline_type, triggered_at)
            existing = (
                db.query(CaseDeadline)
                .filter(
                    CaseDeadline.case_id == case.id,
                    CaseDeadline.deadline_type == deadline_type.value,
                    CaseDeadline.triggered_at == triggered_at,
                )
                .first()
            )
            source_movement_id: Optional[int] = None
            if not isinstance(trigger, date):
                source_movement_id = getattr(trigger, "id", None)

            if existing is None:
                row = CaseDeadline(
                    case_id=case.id,
                    deadline_type=deadline_type.value,
                    legal_basis=deadline_type.legal_basis,
                    due_date=due_date,
                    triggered_at=triggered_at,
                    status="active",
                    source_movement_id=source_movement_id,
                    computed_at=datetime.utcnow(),
                )
                db.add(row)
            else:
                existing.due_date = due_date
                existing.status = "active"
                existing.computed_at = datetime.utcnow()

        db.flush()

        # Step 5: mark superseded — active rows whose triggered_at is no longer current.
        if new_trigger_keys:
            all_active = (
                db.query(CaseDeadline)
                .filter(
                    CaseDeadline.case_id == case.id,
                    CaseDeadline.status == "active",
                )
                .all()
            )
            for row in all_active:
                current_trigger = new_trigger_keys.get(row.deadline_type)
                if current_trigger is None or row.triggered_at != current_trigger:
                    row.status = "superseded"
            db.flush()

        # Step 6: REBELDÍA post-transition.
        # NOTIFICADO + EXCEPCIONES_8D due date elapsed + no EXCEPCIONES movement.
        if proc_state == ProceduralState.NOTIFICADO:
            exc_row = (
                db.query(CaseDeadline)
                .filter(
                    CaseDeadline.case_id == case.id,
                    CaseDeadline.deadline_type == DeadlineType.EXCEPCIONES_8D.value,
                    CaseDeadline.status == "active",
                )
                .first()
            )
            has_excepciones_movement = any(
                (mv.stage or "").strip() == "Excepciones"
                for mv in movements
            )
            if (
                exc_row is not None
                and exc_row.due_date < today
                and not has_excepciones_movement
            ):
                proc_state = ProceduralState.REBELDE
                exc_row.status = "expired"
                db.flush()

        # Step 7a: compute semáforo from nearest active deadline.
        semaforo = cls._compute_semaforo(db, case.id, proc_state, today)

        # Step 7b: abandono and prescripción risk flags (stored on Case in Slice B;
        # for now we compute them but only use for semáforo override decision).
        # The values are not persisted as separate columns in Slice A — they will be
        # part of the GET /deadlines API response in PR2.

        # Step 8: write denormalized Case columns.
        case.procedural_state = proc_state.value
        case.semaforo = semaforo

        # next_deadline_at = earliest active due_date
        nearest = (
            db.query(CaseDeadline.due_date)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.status == "active",
            )
            .order_by(CaseDeadline.due_date.asc())
            .first()
        )
        case.next_deadline_at = nearest[0] if nearest else None

        db.flush()

    # ------------------------------------------------------------------
    # Semáforo computation
    # ------------------------------------------------------------------

    @classmethod
    def _compute_semaforo(
        cls,
        db: Session,
        case_id: int,
        state: ProceduralState,
        today: date,
    ) -> str:
        """Derive semáforo color from state and active/expired deadlines.

        Rules (REQ-5):
          GRIS     → INDETERMINATE state OR TERMINADA OR no deadlines at all.
          ROJO     → nearest active deadline ≤ 1 día hábil remaining,
                     OR any 'expired' deadline exists (past-due, action required).
          AMARILLO → 2–5 días hábiles remaining on nearest active deadline.
          VERDE    → > 5 días hábiles remaining.
        """
        if state in (ProceduralState.INDETERMINATE, ProceduralState.TERMINADA):
            return "gris"

        # Check active deadlines first.
        nearest_active = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case_id,
                CaseDeadline.status == "active",
            )
            .order_by(CaseDeadline.due_date.asc())
            .first()
        )

        if nearest_active is not None:
            remaining = count_business_days_remaining(nearest_active.due_date, today)
            if remaining <= _SEMAFORO_ROJO_MAX:
                return "rojo"
            elif remaining <= _SEMAFORO_AMARILLO_MAX:
                return "amarillo"
            else:
                return "verde"

        # No active deadlines — check if any expired deadline exists.
        # An expired deadline = past-due action (e.g. REBELDÍA, lawyer missed window).
        has_expired = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case_id,
                CaseDeadline.status == "expired",
            )
            .first()
        )
        if has_expired is not None:
            return "rojo"

        return "gris"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _triggered_at_date(trigger: object) -> date:
        """Extract a ``date`` from either a Movement object or a pre-computed date."""
        if isinstance(trigger, date):
            return trigger
        mv_dt = getattr(trigger, "movement_date", None)
        if mv_dt is None:
            raise ValueError(f"Trigger has no movement_date: {trigger!r}")
        if hasattr(mv_dt, "date"):
            return mv_dt.date()
        return mv_dt

    @staticmethod
    def _write_gris(case: Case, db: Session) -> None:
        """Set case to GRIS/INDETERMINATE and flush (safe-fail path)."""
        case.procedural_state = ProceduralState.INDETERMINATE.value
        case.semaforo = "gris"
        case.next_deadline_at = None
        db.flush()
