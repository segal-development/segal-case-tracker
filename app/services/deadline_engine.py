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
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from sqlalchemy.orm import Session

import re as _re

from app.core.deadlines_config import (
    ACTIONABLE_DEADLINE_VALUES,
    DEADLINE_DISCLAIMER,
    DeadlineType,
    MANDATORY_ACTIONABLE_VALUES,
    OPTIONAL_ACTIONABLE_VALUES,
    ProceduralState,
    actionable_sets,
)
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.movement import Movement
from app.services.business_days import add_business_days, count_business_days_remaining
from app.services.procedural_classifier import MovementClassifier

logger = logging.getLogger(__name__)

_CLASSIFIER = MovementClassifier()

_CHILE_TZ = ZoneInfo("America/Santiago")


def _today_chile() -> date:
    """Return today's date in the America/Santiago timezone.

    Using the server's UTC date can produce a 1-day off-by-one during the
    Chilean evening (UTC-3 / UTC-4 depending on DST), which is legally
    significant for deadline boundaries.
    """
    return datetime.now(_CHILE_TZ).date()

# Semáforo thresholds (in días hábiles)
_SEMAFORO_ROJO_MAX = 1    # ≤ 1 → ROJO (0 or negative also ROJO)
_SEMAFORO_AMARILLO_MAX = 5  # 2–5 → AMARILLO; >5 → VERDE

# ---------------------------------------------------------------------------
# Abandono del procedimiento (art. 152/153 CPC) — state classification sets
# ---------------------------------------------------------------------------

_PRE_SENTENCIA_STATES: frozenset[ProceduralState] = frozenset({
    ProceduralState.MANDAMIENTO,
    ProceduralState.NOTIFICADO,
    ProceduralState.EXCEPCIONES,
    ProceduralState.TRASLADO_EJECUTANTE,
    ProceduralState.ADMISIBILIDAD,
    ProceduralState.AUTO_PRUEBA,
    ProceduralState.CITACION_SENTENCIA,
})

_POST_SENTENCIA_STATES: frozenset[ProceduralState] = frozenset({
    ProceduralState.SENTENCIA,
    ProceduralState.REBELDE,
})


def _compute_abandono(
    proc_state: "ProceduralState | None",
    latest_movement_date: "date | None",
    today: date,
) -> bool:
    """Art. 152/153 CPC abandono del procedimiento advisory signal.

    Pre-sentencia states: 6-month inactivity threshold (art. 152 CPC).
    Post-sentencia states: 3-year inactivity threshold (art. 153 inc. 2 CPC).
    TERMINADA, INDETERMINATE, and unrecognised states: always False (excluded).
    """
    if proc_state is None:
        return False
    if latest_movement_date is None:
        return False
    if proc_state in _PRE_SENTENCIA_STATES:
        threshold = latest_movement_date + relativedelta(months=6)
        return today >= threshold
    if proc_state in _POST_SENTENCIA_STATES:
        threshold = latest_movement_date + relativedelta(years=3)
        return today >= threshold
    # TERMINADA, INDETERMINATE, or any unrecognised state
    return False


def _compute_prescripcion(
    case: "Case",
    today: date,
) -> tuple[bool, "Optional[date]"]:
    """Statute-of-limitations advisory signal (Chilean law).

    The prescription clock starts from the DEMAND FILING DATE
    (``Case.filed_at``, "Fecha de ingreso"), NOT the title's vencimiento.

    Plazo by título type:
      {"pagare", "letra", "cheque"} → 1 year
        (art. 98 Ley 18.092 / art. 34 Ley 18.552)
      everything else (None, "otro", "escritura_publica", "sentencia", ...) →
        3 years — the GENERAL acción ejecutiva prescription (art. 2515 CC).
        # 3yr general per art. 2515 CC — confirm exact from-date basis with
        # legal review. Reusing filed_at (the same base date as the 1yr
        # títulos-de-crédito path) is a conservative placeholder pending
        # legal sign-off, not a confirmed art. 2515 CC interpretation.

    Returns (prescripcion_cumplida, prescripcion_fecha).
    If filed_at is None → (False, None) (nothing computable either way).
    prescripcion_cumplida = prescripcion_fecha < today (strictly less than).
    """
    titulo_tipo = (getattr(case, "titulo_tipo", None) or "").lower().strip()

    filed_at = getattr(case, "filed_at", None)
    if filed_at is None:
        return (False, None)

    start = filed_at.date()
    plazo_years = 1 if titulo_tipo in {"pagare", "letra", "cheque"} else 3
    prescripcion_fecha = start + relativedelta(years=plazo_years)
    prescripcion_cumplida = prescripcion_fecha < today
    return (prescripcion_cumplida, prescripcion_fecha)


def _firm_side(db: Session, case: "Case") -> str:
    """Detect which side the firm represents in this case.

    Looks up the lawyer's RUT via the case.lawyer relationship (or FK),
    then checks CaseLitigante rows for a matching RUT:
      - participante "AB.DTE" or "AP.DTE" → firm is demandante (creditor's lawyer)
      - participante "AB.DDO" or "AP.DDO" → firm is demandado (debtor's lawyer)

    Default: "demandado" (the firm primarily defends debtors, ~73%).
    Never raises — any exception returns the safe default.
    """
    try:
        from app.models.case_litigante import CaseLitigante
        from app.models.lawyer import Lawyer
        from app.utils.rut import normalize_rut

        # Resolve lawyer RUT: prefer loaded relationship, fall back to DB lookup.
        lawyer = getattr(case, "lawyer", None)
        if lawyer is None and getattr(case, "lawyer_id", None) is not None:
            lawyer = db.get(Lawyer, case.lawyer_id)
        if lawyer is None:
            return "demandado"
        firm_rut = normalize_rut(lawyer.rut)

        # Collect ALL roles the firm holds, then apply explicit precedence —
        # avoids non-deterministic results if the same RUT appears twice.
        # Normalize both sides: litigante RUTs may carry dots, lawyer RUTs don't.
        roles = {
            lit.participante
            for lit in db.query(CaseLitigante).filter_by(case_id=case.id).all()
            if normalize_rut(lit.rut) == firm_rut
        }
        if roles & {"AB.DTE", "AP.DTE"}:
            return "demandante"
        if roles & {"AB.DDO", "AP.DDO"}:
            return "demandado"
        return "demandado"
    except Exception:
        return "demandado"


@dataclass(frozen=True)
class SemaforoTransition:
    """Before/after snapshot of a single ``recompute_case`` call.

    Pure data — no notification imports here, so ``DeadlineEngine`` stays
    free of any coupling to the alert/notification stack. Callers (e.g.
    ``sync_service.emit_deadline_alerts``) inspect ``entered_rojo`` /
    ``fatal_appeared`` to decide whether to fan out an alert. Comparing the
    OLD vs NEW state (rather than just the new one) makes alerting
    transition-based and naturally de-duped: a case that was already ROJO
    and stays ROJO does not re-fire.
    """

    old_semaforo: Optional[str]
    new_semaforo: Optional[str]
    old_fatal: bool
    new_fatal: bool

    @property
    def entered_rojo(self) -> bool:
        """True only when this recompute is what pushed the case INTO rojo."""
        return self.old_semaforo != "rojo" and self.new_semaforo == "rojo"

    @property
    def fatal_appeared(self) -> bool:
        """True only when ``next_deadline_fatal`` flipped False → True now."""
        return not self.old_fatal and self.new_fatal


class DeadlineEngine:
    """Stateless engine — all state lives in the DB row and is recomputed on sync."""

    @classmethod
    def recompute_case(cls, db: Session, case: Case) -> SemaforoTransition:
        """Recompute procedural deadlines for *case* and flush to DB.

        Flush-only: the outer caller is responsible for commit/rollback.
        Never raises — any exception is logged and GRIS is written to the Case.
        Only processes civil competencia; all others silently become GRIS.

        Args:
            db:   Active SQLAlchemy session (shared with the sync transaction).
            case: Case ORM instance to update (modified in-place + flushed).

        Returns:
            ``SemaforoTransition`` snapshotting ``case.semaforo`` and
            ``case.next_deadline_fatal`` before/after this call. The engine
            itself never alerts — it is the caller's job (see
            ``sync_service.emit_deadline_alerts``) to inspect the transition
            and fan out notifications.
        """
        old_semaforo = case.semaforo
        old_fatal = bool(case.next_deadline_fatal)

        # Apremio detection runs unconditionally so it is set on every code path
        # (success, GRIS safe-fail, and non-civil guard) without touching _write_gris.
        case.en_apremio = cls._compute_en_apremio(db, case)
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

        return SemaforoTransition(
            old_semaforo=old_semaforo,
            new_semaforo=case.semaforo,
            old_fatal=old_fatal,
            new_fatal=bool(case.next_deadline_fatal),
        )

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    @classmethod
    def _recompute_safe(cls, db: Session, case: Case) -> None:
        today = _today_chile()

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

        # Determine the firm's side for this case and derive per-case deadline sets.
        side = _firm_side(db, case)
        mandatory_values, actionable_values = actionable_sets(side)

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

            # Skip rows that are manually managed or already audited
            if existing is not None and (
                existing.is_manual or existing.status in ("cumplido", "no_cumplido")
            ):
                continue

            if existing is None:
                row = CaseDeadline(
                    case_id=case.id,
                    deadline_type=deadline_type.value,
                    legal_basis=deadline_type.legal_basis,
                    due_date=due_date,
                    triggered_at=triggered_at,
                    status="active",
                    source_movement_id=source_movement_id,
                    computed_at=datetime.now(timezone.utc),
                )
                db.add(row)
            else:
                existing.due_date = due_date
                existing.status = "active"
                existing.computed_at = datetime.now(timezone.utc)

        db.flush()

        # Step 5: supersede every active row whose deadline_type is NOT in the
        # current active set (new_trigger_keys).  This runs unconditionally —
        # including when new_trigger_keys is empty (TERMINADA, INDETERMINATE, SENTENCIA
        # after appeal window) — so that no stale "active" rows remain after the
        # case advances, preventing false ROJO from prior-state past-due deadlines.
        all_active = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.status == "active",
                CaseDeadline.is_manual == False,  # noqa: E712
            )
            .all()
        )
        for row in all_active:
            current_trigger = new_trigger_keys.get(row.deadline_type)
            if current_trigger is None or row.triggered_at != current_trigger:
                row.status = "superseded"
        db.flush()

        # Step 5b: OVERTAKEN supersession. An actionable deadline that is past-due
        # AND has court activity (any movement) dated after its due_date has been
        # overtaken — the procedure moved on (often via a movement the classifier
        # doesn't recognise). Mark it superseded so it stops producing a false ROJO.
        # An overdue deadline with NO later movement stays active → still ROJO,
        # because the firm genuinely has a pending action.
        #
        # This applies to MANDATORY deadlines too (incl. the demandado's
        # excepciones): empirically, later court activity reliably means the case
        # is alive / advanced (notification-via-exhorto, mere trámites, etc.), so
        # the lapsed deadline is no longer the live actionable one. A genuinely
        # abandoned missed window (no later movement) still surfaces as ROJO.
        latest_mv_date: Optional[date] = None
        if movements:
            last_mv_dt = movements[-1].movement_date  # movements are ASC
            latest_mv_date = last_mv_dt.date() if hasattr(last_mv_dt, "date") else last_mv_dt
        if latest_mv_date is not None and actionable_values:
            still_active = (
                db.query(CaseDeadline)
                .filter(
                    CaseDeadline.case_id == case.id,
                    CaseDeadline.status == "active",
                    CaseDeadline.deadline_type.in_(actionable_values),
                    CaseDeadline.is_manual == False,  # noqa: E712
                )
                .all()
            )
            for row in still_active:
                if row.due_date < today and latest_mv_date > row.due_date:
                    row.status = "superseded"
            db.flush()

        # Step 6: REBELDÍA post-transition.
        # Only applies when the firm is DEMANDANTE (creditor): the debtor's
        # failure to oppose (EXCEPCIONES_8D lapsed) is a favourable outcome for
        # the ejecutante → flip to REBELDE → proceed to sentencia.
        # For DEMANDADO (debtor's lawyer), EXCEPCIONES_8D is the FIRM's own
        # mandatory deadline; a lapsed window means the firm MISSED its defense,
        # not that the counterparty defaulted. Rebeldía must NOT fire → keep the
        # expired mandatory active so the semáforo stays ROJO.
        if side == "demandante" and proc_state == ProceduralState.NOTIFICADO:
            exc_row = (
                db.query(CaseDeadline)
                .filter(
                    CaseDeadline.case_id == case.id,
                    CaseDeadline.deadline_type == DeadlineType.EXCEPCIONES_8D.value,
                    CaseDeadline.status == "active",
                )
                .first()
            )
            # A REAL opposition only — the previous broad regex (`excepci[oó]n`)
            # matched any mention ("plazo de excepciones", "admisibilidad de
            # excep") and wrongly blocked the rebeldía transition.
            # Real debtor-opposition stages. ("Contestación Excepciones" is the
            # court's traslado resolution — the firm's stage — NOT an opposition,
            # so it is intentionally excluded here.)
            _opposition_stages = {
                "Excepciones",
                "Oposición de Excepciones",
            }
            has_excepciones_movement = any(
                (mv.stage or "").strip() in _opposition_stages
                or bool(_re.search(
                    r"opone\s+excepciones",
                    (mv.description or ""),
                    _re.IGNORECASE,
                ))
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
        semaforo = cls._compute_semaforo(
            db, case.id, proc_state, today,
            actionable_values=actionable_values,
            mandatory_values=mandatory_values,
        )

        # Step 7b: abandono del procedimiento advisory signal (art. 152/153 CPC).
        abandono = _compute_abandono(proc_state, latest_mv_date, today)

        # Step 7c: prescripción advisory signal (statute of limitations).
        prescripcion_cumplida, prescripcion_fecha = _compute_prescripcion(case, today)

        # Step 8: write denormalized Case columns.
        case.procedural_state = proc_state.value
        case.semaforo = semaforo
        case.abandono_disponible = abandono
        case.prescripcion_cumplida = prescripcion_cumplida
        case.prescripcion_fecha = prescripcion_fecha

        # next_deadline_at = the firm's next action date. Mirror the semáforo
        # selection: nearest active ACTIONABLE deadline (per-case side), skipping
        # a past-due OPTIONAL window (closed) — otherwise a VERDE case (e.g.
        # SENTENCIA with the apelación window already closed) would surface a
        # stale past date.
        active_actionable = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.status == "active",
                CaseDeadline.deadline_type.in_(actionable_values),
            )
            .order_by(CaseDeadline.due_date.asc())
            .all()
        )
        case.next_deadline_at = None
        case.next_deadline_fatal = False
        for row in active_actionable:
            if (
                row.deadline_type in OPTIONAL_ACTIONABLE_VALUES
                and count_business_days_remaining(row.due_date, today) < 0
            ):
                continue  # closed optional window — not a pending firm action
            case.next_deadline_at = row.due_date
            try:
                case.next_deadline_fatal = DeadlineType(row.deadline_type).is_fatal
            except ValueError:
                case.next_deadline_fatal = False
            break

        db.flush()

        # Step 9: decision engine — recommended action + next review date.
        # A SEPARATE safe-fail boundary: Steps 1-8 above have already been
        # flushed successfully by this point, so a bug in the (newer,
        # less battle-tested) DecisionEngine must never break the deadline
        # computation that already succeeded.
        cls._recompute_decision(db, case, today)

    @classmethod
    def _recompute_decision(cls, db: Session, case: Case, today: date) -> None:
        """Populate recommended_action_code + next_review_at. NEVER raises.

        Independent safe-fail: any DecisionEngine error clears both columns
        to None rather than propagating (DecisionEngine.recommend and
        .compute_next_review_at already never raise themselves, but this
        boundary guards against future regressions there too).
        """
        try:
            from app.services.decision_engine import DecisionEngine

            # Every DecisionEngine rule is a DEFENSE action (oponer excepciones,
            # solicitar abandono, …). PJUD's litigantes show 431 civil cases where a
            # firm lawyer is the ejecutante's abogado of record — recommending a
            # defense there is nonsense. `_firm_side` answers "demandado" when it
            # cannot tell, so only CONFIRMED plaintiff-side cases are suppressed.
            # `next_review_at` still stands: a prescripción running against our own
            # client is exactly what the plaintiff's lawyer needs to see.
            if _firm_side(db, case) == "demandante":
                case.recommended_action_code = None
            else:
                recommendation = DecisionEngine.recommend(case, today=today)
                case.recommended_action_code = (
                    recommendation.code if recommendation else None
                )
            case.next_review_at = DecisionEngine.compute_next_review_at(case, today=today)
        except Exception as exc:
            logger.exception(
                "DecisionEngine step failed for case_id=%s: %s — clearing "
                "recommended_action_code/next_review_at",
                getattr(case, "id", "?"),
                exc,
            )
            case.recommended_action_code = None
            case.next_review_at = None
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
        *,
        actionable_values: frozenset[str] = ACTIONABLE_DEADLINE_VALUES,
        mandatory_values: frozenset[str] = MANDATORY_ACTIONABLE_VALUES,
    ) -> str:
        """Derive semáforo color from the FIRM's own (actionable) deadlines only.

        Color rules (side-aware via ``actionable_values`` / ``mandatory_values``):
          GRIS     → INDETERMINATE or TERMINADA.
          ROJO     → nearest active ACTIONABLE deadline ≤ 1 día hábil remaining,
                     OR a MANDATORY deadline is expired (firm missed it).
          AMARILLO → 2–5 días hábiles remaining on nearest active actionable.
          VERDE    → > 5 días hábiles remaining, OR nothing pending for the firm
                     (rebeldía for demandante, awaiting court sentencia, or an
                     optional window that already closed).
        Deadlines outside the firm's actionable set for the given side and expired
        OPTIONAL_ACTIONABLE (APELACION_5D) NEVER by themselves produce ROJO.
        """
        if state in (ProceduralState.INDETERMINATE, ProceduralState.TERMINADA):
            return "gris"

        # Only the firm's actionable deadlines drive the color. Pick the nearest
        # one that still "counts": a past-due OPTIONAL window (apelación) is
        # closed, not red, so skip it.
        active_actionable = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case_id,
                CaseDeadline.status == "active",
                CaseDeadline.deadline_type.in_(actionable_values),
            )
            .order_by(CaseDeadline.due_date.asc())
            .all()
        )
        for row in active_actionable:
            remaining = count_business_days_remaining(row.due_date, today)
            if row.deadline_type in OPTIONAL_ACTIONABLE_VALUES and remaining < 0:
                continue  # optional window already closed → not red
            if remaining <= _SEMAFORO_ROJO_MAX:
                return "rojo"
            elif remaining <= _SEMAFORO_AMARILLO_MAX:
                return "amarillo"
            else:
                return "verde"

        # No active actionable deadline — only a MISSED MANDATORY action is red.
        # (For demandante: expired traslado_ejecutante = firm missed reply.
        #  For demandado:  expired excepciones = firm missed the defense window.
        #  Expired apelación = closed optional window → not red.)
        has_expired_mandatory = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case_id,
                CaseDeadline.status == "expired",
                CaseDeadline.deadline_type.in_(mandatory_values),
            )
            .first()
        )
        if has_expired_mandatory is not None:
            return "rojo"

        # Nothing pending for the firm → al día.
        return "verde"

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

    @classmethod
    def _compute_en_apremio(cls, db: Session, case: "Case") -> bool:
        """Detect whether any movement signals the cuaderno de apremio phase.

        Returns True when ANY movement has:
          - stage containing 'apremio' or 'remate' (case-insensitive), OR
          - description containing 'embargo', 'remate', or 'martillero' (case-insensitive)

        Never raises — returns False on any error.
        """
        try:
            # Reuse loaded relationship if already in instance dict; otherwise query.
            if "movements" in case.__dict__:
                movements = case.__dict__["movements"]
            else:
                movements = (
                    db.query(Movement)
                    .filter(Movement.case_id == case.id)
                    .all()
                )
            for mv in movements:
                stage = (mv.stage or "").lower()
                desc = (mv.description or "").lower()
                if "apremio" in stage or "remate" in stage:
                    return True
                if "embargo" in desc or "remate" in desc or "martillero" in desc:
                    return True
            return False
        except Exception:
            return False

    @staticmethod
    def _write_gris(case: Case, db: Session) -> None:
        """Set case to GRIS/INDETERMINATE and flush (safe-fail path)."""
        case.procedural_state = ProceduralState.INDETERMINATE.value
        case.semaforo = "gris"
        case.next_deadline_at = None
        case.abandono_disponible = False
        case.next_deadline_fatal = False
        case.prescripcion_cumplida = False
        case.prescripcion_fecha = None
        case.recommended_action_code = None
        case.next_review_at = None
        db.flush()
