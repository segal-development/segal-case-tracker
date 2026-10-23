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
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

import re as _re
import unicodedata

from app.core.deadlines_config import (
    ACTIONABLE_DEADLINE_VALUES,
    FATAL_DEADLINE_VALUES,
    DEADLINE_DISCLAIMER,
    DeadlineType,
    MANDATORY_ACTIONABLE_VALUES,
    OPTIONAL_ACTIONABLE_VALUES,
    PARALLEL_DEADLINE_VALUES,
    ProceduralState,
    actionable_sets,
)
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_escrito import CaseEscrito
from app.models.movement import Movement
from app.services.business_days import add_business_days, count_business_days_remaining
from app.services.deadline_verdict import (
    DETECTORS,
    Verdict,
    default_margin_days,
    evaluate_deadline,
    is_annulled,
    is_excepciones_escrito,
    verdict_rank,
    verdict_uses_escritos,
)
from app.services.poder_deadline import find_poder_obligation
from app.services.procedural_classifier import MovementClassifier, anchor_date, due_anchor_date

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


# A REAL debtor opposition only. The previous broad regex (`excepci[oó]n`)
# matched any mention ("plazo de excepciones", "admisibilidad de excep") and
# wrongly blocked the rebeldía transition. ("Contestación Excepciones" is the
# court's traslado resolution, the creditor's stage: NOT an opposition.)
_OPPOSITION_STAGES = frozenset({"Excepciones", "Oposición de Excepciones"})
_OPONE_EXCEPCIONES_RE = _re.compile(r"opone\s+excepciones", _re.IGNORECASE)
# Who files an escrito for the ejecutado. Matched on whole words after
# normalisation, so "Demandante" never matches.
_DEMANDADO_SOLICITANTE_RE = _re.compile(r"\b(demandad[oa]s?|ejecutad[oa]s?)\b")


def _is_opposition_movement(mv: Movement) -> bool:
    return (mv.stage or "").strip() in _OPPOSITION_STAGES or bool(
        _OPONE_EXCEPCIONES_RE.search(mv.description or "")
    )


def _normalize_text(value: Optional[str]) -> str:
    """Lowercase, accent-free, single-spaced text for tolerant comparison."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


_MEDIDA_PREJUDICIAL = "medida prejudicial"


def is_medida_prejudicial(procedure: Optional[str]) -> bool:
    """True when a case's ``procedure`` is exactly 'Medida prejudicial'.

    The firm is always the solicitante there and does not run the ejecutivo
    flow, so such a row is neutral (see ``DeadlineEngine.neutralize_medida_prejudicial``).
    Compared after ``_normalize_text`` (case, accents, whitespace); None and
    blank (or non-string) are NOT a medida prejudicial.
    """
    return isinstance(procedure, str) and _normalize_text(procedure) == _MEDIDA_PREJUDICIAL


def medida_prejudicial_clause():
    """SQL twin of ``is_medida_prejudicial`` on ``Case.procedure``.

    Case- and edge-whitespace-insensitive (accents and inner runs of spaces are
    left to the Python predicate: the stored value has no such variants).
    NULL procedure yields False, never NULL, so ``~medida_prejudicial_clause()``
    keeps the NULL rows.
    """
    return func.lower(func.trim(func.coalesce(Case.procedure, ""))) == _MEDIDA_PREJUDICIAL


def _is_demandado_opposition_escrito(escrito: CaseEscrito) -> bool:
    """True for a dated escrito in which the ejecutado opposes the execution.

    One definition, shared with the verdict (``deadline_verdict``).
    """
    return is_excepciones_escrito(escrito)


class _EscritosOfCase:
    """The ``case_escritos`` of one case, loaded lazily and AT MOST ONCE.

    A recompute touches several plazo rows of the same case; each used to be a
    candidate for its own query. Cases that never need escritos (no excepciones
    verdict to compute) never pay the query.
    """

    def __init__(self, db: Session, case_id: int) -> None:
        self._db = db
        self._case_id = case_id
        self._rows: Optional[list[CaseEscrito]] = None

    def get(self) -> list[CaseEscrito]:
        if self._rows is None:
            self._rows = (
                self._db.query(CaseEscrito)
                .filter(CaseEscrito.case_id == self._case_id)
                .all()
            )
        return self._rows


# Verdicts an escrito can still improve (a better one is never replaced).
_REJUDGEABLE_VERDICTS = ("no_cumplido", "fuera_de_plazo", "registro_tardio")


def _day_of(value: object) -> date:
    return value.date() if isinstance(value, datetime) else value  # type: ignore[return-value]


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


@dataclass
class LoadedMovements:
    """Out-parameter for ``DeadlineEngine.recompute_case``.

    The engine already loads ALL of a case's movements to classify them. A
    caller that needs the same rows right after (the matriz classification in
    the sync pipeline) passes one of these and reads ``movements`` back instead
    of paying a second round-trip. ``movements`` stays ``None`` when the engine
    did not get as far as loading them (non-civil case, early failure) - the
    caller must then query for itself.
    """

    movements: "Optional[list[Movement]]" = None


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
    def recompute_case(
        cls,
        db: Session,
        case: Case,
        *,
        loaded: Optional[LoadedMovements] = None,
    ) -> SemaforoTransition:
        """Recompute procedural deadlines for *case* and flush to DB.

        Flush-only: the outer caller is responsible for commit/rollback.
        Never raises — any exception is logged and GRIS is written to the Case.
        Only processes civil competencia; all others silently become GRIS.

        Args:
            db:   Active SQLAlchemy session (shared with the sync transaction).
            case: Case ORM instance to update (modified in-place + flushed).
            loaded: Optional out-parameter; receives the case's movements as
                    loaded by the engine (see ``LoadedMovements``).

        Returns:
            ``SemaforoTransition`` snapshotting ``case.semaforo`` and
            ``case.next_deadline_fatal`` before/after this call. The engine
            itself never alerts — it is the caller's job (see
            ``sync_service.emit_deadline_alerts``) to inspect the transition
            and fan out notifications.
        """
        old_semaforo = case.semaforo
        old_fatal = bool(case.next_deadline_fatal)

        # A medida prejudicial is neutral: no flow, no plazos, no apremio.
        if is_medida_prejudicial(case.procedure):
            cls.neutralize_medida_prejudicial(db, case)
            return SemaforoTransition(
                old_semaforo=old_semaforo,
                new_semaforo=case.semaforo,
                old_fatal=old_fatal,
                new_fatal=bool(case.next_deadline_fatal),
            )

        # Apremio detection runs unconditionally so it is set on every code path
        # (success, GRIS safe-fail, and non-civil guard) without touching _write_gris.
        case.en_apremio = cls._compute_en_apremio(db, case)
        # Transient (NOT a DB column): the apremio sub-stage refines the
        # recommendation only. Recomputed here so recommend() can read it
        # within this pipeline; only the resulting recommended_action_code
        # persists. None when not in remate → generic apremio behaviour.
        case._apremio_substage = (
            cls._compute_apremio_substage(db, case) if case.en_apremio else None
        )
        try:
            cls._recompute_safe(db, case, loaded=loaded)
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
    def _recompute_safe(
        cls, db: Session, case: Case, loaded: Optional[LoadedMovements] = None
    ) -> None:
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
        if loaded is not None:
            loaded.movements = movements

        escritos = _EscritosOfCase(db, case.id)

        # Step 3: classify.
        proc_state, triggers = _CLASSIFIER.classify(movements, today)

        # Determine the firm's side for this case and derive per-case deadline sets.
        side = _firm_side(db, case)
        mandatory_values, actionable_values = actionable_sets(side)
        # `side` is resolved ONCE per recompute and handed to the decision step
        # below (it used to query the litigantes a second time).

        # Step 4: upsert case_deadlines for each active trigger.
        new_trigger_keys: dict[str, date] = {}  # deadline_type → triggered_at

        for deadline_type, trigger in triggers.items():
            triggered_at = cls._triggered_at_date(trigger)
            # triggered_at (the row identity) stays the notification's; only the
            # due date counts from the requerimiento de pago for excepciones_8d.
            due_date = add_business_days(
                triggered_at
                if isinstance(trigger, date)
                else due_anchor_date(deadline_type, trigger, movements),
                deadline_type.dias_habiles,
            )

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
                # A live plazo has no verdict yet; one left from an earlier
                # closure (row revived from superseded) would be stale.
                cls._clear_verdict(existing)

        db.flush()

        # Step 5: supersede every active row whose deadline_type is NOT in the
        # current active set (new_trigger_keys).  This runs unconditionally —
        # including when new_trigger_keys is empty (TERMINADA, INDETERMINATE, SENTENCIA
        # after appeal window) — so that no stale "active" rows remain after the
        # case advances, preventing false ROJO from prior-state past-due deadlines.
        # The same query also returns the closed excepciones rows an escrito could
        # still correct (see ``_rejudge_closed_excepciones``), so re-judging them
        # costs no extra round-trip.
        all_active = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.is_manual == False,  # noqa: E712
                # PARALLEL plazos are not in the classifier's triggers by design;
                # without this exclusion they would be superseded on the next
                # recompute (silently: no error, no log). Owned by step 5c.
                CaseDeadline.deadline_type.notin_(PARALLEL_DEADLINE_VALUES),
                or_(
                    CaseDeadline.status == "active",
                    # A plazo step 6b closed ("expired") is still ours to
                    # supersede when the case advances: left alone it would
                    # keep the semáforo ROJO for a state that no longer exists.
                    and_(
                        CaseDeadline.status == "expired",
                        CaseDeadline.marked_by.is_(None),
                        CaseDeadline.marked_at.is_(None),
                    ),
                    and_(
                        CaseDeadline.deadline_type == DeadlineType.EXCEPCIONES_8D.value,
                        # "no_cumplido": the engine's own late-escrito miss. An
                        # auditor's mark carries marked_by/marked_at, excluded here.
                        CaseDeadline.status.in_(("superseded", "expired", "no_cumplido")),
                        CaseDeadline.marked_by.is_(None),
                        CaseDeadline.marked_at.is_(None),
                        # NULL verdict is excluded on purpose: a closed row without
                        # one was cleared (re-anchored, stale due_date) and judging
                        # it would invent a miss.
                        CaseDeadline.verdict.in_(_REJUDGEABLE_VERDICTS),
                    ),
                ),
            )
            .all()
        )
        # "expired" rows are re-derived like live ones, except the excepciones
        # rebeldía closure (demandante), which is final by design and only
        # re-judged against escritos.
        def _is_rebeldia_closure(r: CaseDeadline) -> bool:
            return (
                r.status == "expired"
                and r.deadline_type == DeadlineType.EXCEPCIONES_8D.value
                and side == "demandante"
            )

        closed_candidates = [
            r for r in all_active
            if r.status in ("superseded", "no_cumplido") or _is_rebeldia_closure(r)
        ]
        all_active = [r for r in all_active if r not in closed_candidates]
        for row in all_active:
            current_trigger = new_trigger_keys.get(row.deadline_type)
            # DEMANDADO: a filed defense is judged FIRST. It must win over every
            # supersession below (the case moved on, or later court activity),
            # which would otherwise drop the plazo as "superseded" and lose the
            # fact that the firm did (or did not) answer in time. A re-anchored
            # row is left to its replacement: its due_date is stale.
            re_anchored = (
                current_trigger is not None and row.triggered_at != current_trigger
            )
            if (
                side == "demandado"
                and row.deadline_type == DeadlineType.EXCEPCIONES_8D.value
                and not re_anchored
                and cls._close_excepciones_on_defense(db, row, movements, today, escritos)
            ):
                continue
            if current_trigger is None:
                # The plazo closed because the case moved on: judge it BEFORE
                # superseding, or the evidence is lost with the row's status.
                cls._record_verdict(row, movements, today, escritos)
                row.status = "superseded"
            elif row.triggered_at != current_trigger:
                # Re-anchored (e.g. the diligencia-date change): a NEW row for
                # the same plazo carries the verdict. This row's due_date is
                # stale, so a verdict on it would be wrong by construction.
                cls._clear_verdict(row)
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
                    cls._record_verdict(row, movements, today, escritos)
                    row.status = "superseded"
            db.flush()

        # Step 5c: parallel plazos (outside the "one state, one set of plazos"
        # model). Runs AFTER steps 5/5b so nothing above can touch these rows.
        cls._sync_parallel_deadlines(db, movements, today)
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
            # A REAL opposition only (see _is_opposition_movement).
            has_excepciones_movement = any(
                _is_opposition_movement(mv) for mv in movements
            )
            if (
                exc_row is not None
                and exc_row.due_date < today
                and not has_excepciones_movement
            ):
                proc_state = ProceduralState.REBELDE
                cls._record_verdict(exc_row, movements, today, escritos)
                exc_row.status = "expired"  # rebeldía; also used by step 6b
                db.flush()

        # Step 6b: an overdue plazo that is still active (nothing moved on yet)
        # is already a closed question: record it now instead of waiting for it
        # to be superseded. A plazo still in force stays sin determinar.
        #
        # Once its term (publication margin included) is over, the row is also
        # CLOSED as "expired". Leaving it "active" made a plazo from months ago
        # drive ``next_deadline_at`` and the semáforo forever. "expired" keeps
        # the meaning the semáforo already gives it ("a MANDATORY plazo lapsed
        # => ROJO"), so a missed obligation is not silently turned green. Rows
        # an auditor touched are left alone.
        overdue_active = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.status == "active",
                CaseDeadline.is_manual == False,  # noqa: E712
                CaseDeadline.due_date < today,
            )
            .all()
        )
        for row in overdue_active:
            cls._record_verdict(row, movements, today, escritos)
            if (
                row.status == "active"
                and row.marked_by is None
                and row.marked_at is None
                and cls._term_elapsed(row, today, movements)
            ):
                row.status = "expired"
        db.flush()

        # Step 6c: a plazo closed BEFORE its escrito was known keeps a stale miss
        # forever unless it is judged again.
        cls._rejudge_closed_excepciones(db, movements, today, escritos, closed_candidates)

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
        case.semaforo = cls._apply_semaforo_override(case, semaforo)
        case.abandono_disponible = abandono
        case.prescripcion_cumplida = prescripcion_cumplida
        case.prescripcion_fecha = prescripcion_fecha

        # next_deadline_at = the firm's next action date. Mirror the semáforo
        # selection: nearest active ACTIONABLE deadline (per-case side), skipping
        # a past-due OPTIONAL window (closed) — otherwise a VERDE case (e.g.
        # SENTENCIA with the apelación window already closed) would surface a
        # stale past date.
        #
        # ONE query serves both outputs (every statement is a Cloud SQL
        # round-trip, see tests/services/test_roundtrips_per_case.py): the
        # active rows drive next_deadline_at, the no_cumplido ones drive the
        # compliance flag.
        actionable_rows = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.status.in_(("active", "no_cumplido")),
                CaseDeadline.deadline_type.in_(actionable_values),
            )
            .order_by(CaseDeadline.due_date.asc())
            .all()
        )
        active_actionable = [r for r in actionable_rows if r.status == "active"]
        # Compliance axis: a lost FATAL plazo leaves the active set, so the
        # semáforo cannot show it; this flag keeps it visible. Recomputed every
        # time (never accumulated): an auditor correcting the row clears it.
        case.plazo_fatal_incumplido = any(
            r.status == "no_cumplido" and r.deadline_type in FATAL_DEADLINE_VALUES
            for r in actionable_rows
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
        cls._recompute_decision(db, case, today, side=side)

    @classmethod
    def _close_excepciones_on_defense(
        cls,
        db: Session,
        row: CaseDeadline,
        movements: list[Movement],
        today: date,
        escritos_of_case: Optional[_EscritosOfCase] = None,
    ) -> bool:
        """DEMANDADO: a filed "Opone excepciones" fulfils the art. 464 plazo.

        The evidence is the ``case_escritos`` row (the filing exists in our
        data long before the court provee it as a movement); a movement that
        shows the opposition is accepted as a fallback. Only a live, automatic
        row is touched: manual rows and rows an auditor judged are never
        overwritten (same guard as ``_record_verdict``).

        The on-time test uses the EARLIEST evidence date (see
        ``_earliest_defense_evidence``): an opposition movement is dated when
        PJUD PUBLISHED it, so the escrito was filed that day or before. That
        proves ON TIME, never late.

        * earliest evidence on or before due_date, escrito on file -> status
          ``cumplido``
        * every escrito after due_date, no earlier movement -> status
          ``no_cumplido`` (the audit trail must say it was late). Both leave
          the active set, so neither keeps the semáforo red: nothing is
          pending for the firm.
        * movement-only evidence: published after due_date it cannot prove
          late (publication lag), so the row is closed as ``superseded`` (the
          engine's long-standing treatment) and the margin-aware verdict says
          what can honestly be claimed. Published on or before due_date the
          closure is the same, but the verdict is ``cumplido``.
        """
        if row.is_manual or row.status != "active":
            return False
        if escritos_of_case is None:
            escritos_of_case = _EscritosOfCase(db, row.case_id)
        escritos = [
            _day_of(e.fecha_ingreso)
            for e in escritos_of_case.get()
            if _is_demandado_opposition_escrito(e)
        ]
        opposition_movements = [mv for mv in movements if _is_opposition_movement(mv)]
        if not escritos and not opposition_movements:
            return False

        # Judged against the due_date the plazo SHOULD have (a stale row may
        # still carry the pre-diligencia one); the row's own field is not edited.
        due = cls._current_due_date(row, movements)
        earliest = cls._earliest_defense_evidence(
            escritos, opposition_movements, row.triggered_at
        )
        if earliest is not None and earliest[0] <= due:
            if escritos:
                row.status = "cumplido"
            else:
                row.status = "superseded"  # movement-only: long-standing closure
            cls._write_verdict(row, Verdict.CUMPLIDO, earliest[1], earliest[0])
        elif escritos:
            row.status = "no_cumplido"
            cls._write_verdict(row, Verdict.FUERA_DE_PLAZO, None, min(escritos))
        else:
            # Same closure the engine always gave a movement-shown opposition
            # (superseded + margin-aware verdict).
            row.status = "superseded"
            cls._apply_verdict(row, movements, today, escritos_of_case)
        row.computed_at = datetime.now(timezone.utc)
        return True

    @classmethod
    def _rejudge_closed_excepciones(
        cls,
        db: Session,
        movements: list[Movement],
        today: date,
        escritos: _EscritosOfCase,
        rows: list[CaseDeadline],
    ) -> None:
        """Re-judge CLOSED excepciones rows against the evidence now on file.

        The filing date (``case_escritos.fecha_ingreso``) can arrive after the
        row was closed (superseded / expired) with a movement-only verdict, and
        a closed row is never visited by the live-plazo steps again. ``rows``
        are pre-selected by the step-5 query (excepciones, closed, automatic,
        not marked by an auditor, verdict a miss). Opposition movements are
        evidence too, but only of being ON TIME (see
        ``_earliest_defense_evidence``). Rules:

        * ``verdict*`` is rewritten; ``status`` is changed ONLY for the engine's
          own late-escrito miss (``no_cumplido`` / ``fuera_de_plazo``), and only
          to ``cumplido`` when the earliest evidence is within the plazo. It
          is left alone otherwise.
        * Monotone: the verdict is replaced ONLY by a strictly better one, so
          this can never turn a ``cumplido`` into a miss.
        * Auditor decisions always win: manual rows, rows with ``marked_by`` /
          ``marked_at`` never reach this method, and rows whose status an
          auditor set (cumplido / no_cumplido) are not closed rows.
        * Escritos are loaded only when there is a row to judge.
        """
        if not rows:
            return
        try:
            escrito_days = [
                _day_of(e.fecha_ingreso)
                for e in escritos.get()
                if _is_demandado_opposition_escrito(e)
            ]
            opposition_movements = [mv for mv in movements if _is_opposition_movement(mv)]
            for row in rows:
                if row.status == "no_cumplido":
                    cls._correct_late_miss(row, escrito_days, opposition_movements, movements)
                    continue
                result = evaluate_deadline(
                    row.deadline_type,
                    cls._current_due_date(row, movements),
                    movements,
                    today,
                    triggered_at=row.triggered_at,
                    escritos=escritos.get(),
                )
                verdict, movement_id, acted_on = (
                    result.verdict, result.movement_id, result.acted_on
                )
                # An opposition movement published within the plazo proves it
                # was met whatever its "Trámite" (the detector wants "Escrito").
                earliest = cls._earliest_defense_evidence(
                    escrito_days, opposition_movements, row.triggered_at
                )
                if earliest is not None and earliest[0] <= cls._current_due_date(row, movements):
                    verdict, movement_id, acted_on = Verdict.CUMPLIDO, earliest[1], earliest[0]
                if verdict is None or verdict_rank(verdict) <= verdict_rank(row.verdict):
                    continue
                cls._write_verdict(row, verdict, movement_id, acted_on)
            db.flush()
        except Exception as exc:
            logger.exception("re-judging closed excepciones failed: %s — skipping", exc)

    @classmethod
    def _correct_late_miss(
        cls,
        row: CaseDeadline,
        escrito_days: list[date],
        opposition_movements: list[Movement],
        movements: list[Movement],
    ) -> None:
        """Upgrade the engine's own late-escrito miss to ``cumplido`` if disproved.

        ``_close_excepciones_on_defense`` closes a late escrito as ``no_cumplido``
        / ``fuera_de_plazo``; no other step revisits such a row. Only that exact
        engine-set shape is touched (the caller's query already excluded manual
        and auditor-marked rows; a miss without the ``fuera_de_plazo`` verdict
        has unknown provenance). Upgrade-only: with no evidence on or before the
        plazo the row is left exactly as it is, except that its ``due_date`` is
        refreshed to the one it should have (the plazo may now count from the
        requerimiento de pago, later than the cédula the row was built on).
        """
        if row.status != "no_cumplido" or row.verdict != Verdict.FUERA_DE_PLAZO.value:
            return
        due = cls._current_due_date(row, movements)
        row.due_date = due
        earliest = cls._earliest_defense_evidence(
            escrito_days, opposition_movements, row.triggered_at
        )
        if earliest is None or earliest[0] > due:
            return
        row.status = "cumplido"
        cls._write_verdict(row, Verdict.CUMPLIDO, earliest[1], earliest[0])
        row.computed_at = datetime.now(timezone.utc)

    @staticmethod
    def _earliest_defense_evidence(
        escrito_days: list[date],
        opposition_movements: list[Movement],
        triggered_at: Optional[date],
    ) -> Optional[tuple[date, Optional[int]]]:
        """(date, movement id) of the earliest proof the defense was filed.

        An escrito's ``fecha_ingreso`` is the filing date; an opposition
        movement's date is its PUBLICATION date, so the filing was that day or
        earlier: both bound the filing date from above and the smaller wins.
        On a tie the escrito wins (movement id None). An annulled
        ("[Nulo]") movement, or one before ``triggered_at`` (it predates the
        plazo), is not evidence. ``None`` when
        there is no evidence at all.
        """
        best: Optional[tuple[date, Optional[int]]] = (
            (min(escrito_days), None) if escrito_days else None
        )
        for mv in opposition_movements:
            day = _day_of(mv.movement_date)
            if is_annulled(mv) or (triggered_at is not None and day < triggered_at):
                continue
            if best is None or day < best[0]:
                best = (day, mv.id)
        return best

    @staticmethod
    def _write_verdict(
        row: CaseDeadline,
        verdict: Verdict,
        movement_id: Optional[int],
        acted_on: date,
    ) -> None:
        row.verdict = verdict.value
        row.verdict_movement_id = movement_id
        row.verdict_acted_on = acted_on
        row.verdict_computed_at = datetime.now(timezone.utc)

    @staticmethod
    def _clear_verdict(row: CaseDeadline) -> None:
        row.verdict = None
        row.verdict_movement_id = None
        row.verdict_acted_on = None
        row.verdict_computed_at = None

    @classmethod
    def _record_verdict(
        cls,
        row: CaseDeadline,
        movements: list[Movement],
        today: date,
        escritos: Optional[_EscritosOfCase] = None,
    ) -> None:
        """Persist the verdict of a plazo that is closing (or already overdue).

        Separate from ``status`` on purpose. Auditor marks always win: manual
        rows and rows the auditor set to cumplido / no_cumplido are never
        touched. Types without a detector stay sin determinar.
        """
        if row.is_manual or row.status in ("cumplido", "no_cumplido"):
            return
        cls._apply_verdict(row, movements, today, escritos)

    @classmethod
    def _apply_verdict(
        cls,
        row: CaseDeadline,
        movements: list[Movement],
        today: date,
        escritos: Optional[_EscritosOfCase] = None,
    ) -> None:
        """Evaluate and write the verdict columns of ``row``. NEVER raises.

        A verdict is derived data: failing to compute it must never break the
        sync, so any error is logged and the row is left without a verdict
        (a stale one would be worse than none). Writes ONLY ``verdict*``.
        """
        try:
            result = evaluate_deadline(
                row.deadline_type,
                cls._current_due_date(row, movements),
                movements,
                today,
                triggered_at=row.triggered_at,
                escritos=(
                    escritos.get()
                    if escritos is not None and verdict_uses_escritos(row.deadline_type)
                    else None
                ),
            )
            if result.verdict is None:
                cls._clear_verdict(row)
                return
            row.verdict = result.verdict.value
            row.verdict_movement_id = result.movement_id
            row.verdict_acted_on = result.acted_on
            row.verdict_computed_at = datetime.now(timezone.utc)
        except Exception as exc:
            logger.exception(
                "verdict failed for case_id=%s deadline_type=%s: %s — skipping",
                getattr(row, "case_id", "?"), getattr(row, "deadline_type", "?"), exc,
            )
            cls._clear_verdict(row)

    @classmethod
    def _term_elapsed(
        cls,
        row: CaseDeadline,
        today: date,
        movements: Optional[list[Movement]] = None,
    ) -> bool:
        """Whether ``row`` is genuinely finished, so it may leave the active set.

        Judged on the EFFECTIVE due date (``_current_due_date``), never the raw
        column, which can be stale on a re-anchored row. Types with a verdict
        detector also wait out the publication margin: an act filed on time can
        be published late, and closing the row earlier would freeze a false
        miss (the bug fixed in PR #379). Types without a detector have no
        verdict to protect, so their term ends on the due date.
        """
        margin = default_margin_days(row.deadline_type) if row.deadline_type in DETECTORS else 0
        return cls._current_due_date(row, movements) + timedelta(days=margin) < today

    @staticmethod
    def _current_due_date(
        row: CaseDeadline, movements: Optional[list] = None
    ) -> date:
        """due_date the plazo SHOULD have today.

        A row created before the diligencia-date anchor change can still carry
        the old due_date when it closes (it is never revisited once the type
        leaves the triggers). Re-derive it from the source notification so the
        verdict is judged against the current rule. Row fields are not touched.

        ``movements`` (the case's) lets ``excepciones_8d`` count from the
        requerimiento de pago (see ``due_anchor_date``); without them the
        notification anchor is used, as before.
        """
        source = row.source_movement
        if source is None:
            return row.due_date
        try:
            dtype = DeadlineType(row.deadline_type)
        except ValueError:
            return row.due_date
        return add_business_days(
            due_anchor_date(dtype, source, movements or []), dtype.dias_habiles
        )

    @classmethod
    def _sync_parallel_deadlines(
        cls, db: Session, movements: list, today: date
    ) -> None:
        """Upsert ACREDITAR_PODER_3D from the raw movements. NEVER raises.

        Own safe-fail boundary: a bug here must not push the whole case to GRIS
        or disturb the fatal plazos computed by the classifier path.

        Parallel on purpose (see ``PARALLEL_DEADLINES``): it does not move the
        procedural state and is not an actionable plazo, so it never drives the
        semáforo or ``next_deadline_at``. Its alert (only when the obligation is
        provably ours) is emitted separately by
        ``sync_service.emit_ratification_alert``.

        CONSCIOUS DEBT: when a SECOND parallel plazo shows up, refactor
        ``ClassifierRule`` (optional next_state + parallel triggers) instead of
        adding a third code path next to this one.
        """
        try:
            if not movements:
                return
            case_id = movements[0].case_id
            dtype = DeadlineType.ACREDITAR_PODER_3D
            obligation = find_poder_obligation(movements)

            current_trigger: Optional[date] = None
            if obligation is not None:
                current_trigger = cls._triggered_at_date(obligation.trigger)
                due_date = add_business_days(current_trigger, dtype.dias_habiles)
                existing = (
                    db.query(CaseDeadline)
                    .filter(
                        CaseDeadline.case_id == case_id,
                        CaseDeadline.deadline_type == dtype.value,
                        CaseDeadline.triggered_at == current_trigger,
                    )
                    .first()
                )
                current_row = existing
                if existing is None:
                    current_row = CaseDeadline(
                        case_id=case_id,
                        deadline_type=dtype.value,
                        legal_basis=dtype.legal_basis,
                        due_date=due_date,
                        triggered_at=current_trigger,
                        status="cumplido" if obligation.fulfilled else "active",
                        source_movement_id=getattr(obligation.trigger, "id", None),
                        computed_at=datetime.now(timezone.utc),
                    )
                    db.add(current_row)
                elif not existing.is_manual and existing.status in ("active", "expired"):
                    # Only "active" rows (and "expired", which step 6b writes
                    # for a lapsed plazo and re-derives every run, so a late
                    # act can still turn it cumplido) are refreshed. Audited (cumplido /
                    # no_cumplido) and manual rows are the auditor's call, and a
                    # "superseded" row is deliberately NOT resurrected: that keeps
                    # the step-5 exclusion the single guard against silent loss
                    # (a self-healing upsert here would mask a broken exclusion).
                    existing.due_date = due_date
                    existing.status = "cumplido" if obligation.fulfilled else "active"
                    existing.computed_at = datetime.now(timezone.utc)

                # The verdict is separate from ``status`` (which Sysgal reads and
                # which is written above without comparing dates; it is NOT
                # touched here). Auditor-marked and manual rows stay theirs. A
                # status="cumplido" written by the engine itself carries no
                # marked_by/marked_at, so it is judged too.
                if (
                    not current_row.is_manual
                    and current_row.marked_by is None
                    and current_row.marked_at is None
                ):
                    cls._apply_verdict(current_row, movements, today)

            # Any other still-active automatic row is stale (a newer resolution
            # replaced it, or the only one was annulled).
            stale = (
                db.query(CaseDeadline)
                .filter(
                    CaseDeadline.case_id == case_id,
                    CaseDeadline.deadline_type == dtype.value,
                    CaseDeadline.status.in_(("active", "expired")),
                    CaseDeadline.is_manual == False,  # noqa: E712
                )
                .all()
            )
            for row in stale:
                if current_trigger is None or row.triggered_at != current_trigger:
                    # Judged against a newer resolution's movements the old row
                    # could read a false cumplido: leave it without a verdict.
                    cls._clear_verdict(row)
                    row.status = "superseded"
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.exception(
                "parallel deadline sync failed for case_id=%s: %s — skipping",
                getattr(movements[0], "case_id", "?") if movements else "?",
                exc,
            )

    @classmethod
    def _recompute_decision(
        cls, db: Session, case: Case, today: date, side: Optional[str] = None
    ) -> None:
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
            if side is None:
                side = _firm_side(db, case)
            if side == "demandante":
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

    @staticmethod
    def _apply_semaforo_override(case: Case, computed: str) -> str:
        """Honor a manual liberación override until a NEWER movement supersedes it.

        A liberación (auditor + dirección dual sign-off) pins the case's semáforo
        to a chosen range. It holds while no case activity arrived after it; once a
        newer movement lands (last_movement_at > override_at) the override is
        cleared and the computed color resumes — "el semáforo se sigue moviendo".
        """
        override = getattr(case, "semaforo_override", None)
        if not override:
            return computed
        ov_at = getattr(case, "semaforo_override_at", None)
        last_mv = getattr(case, "last_movement_at", None)
        if ov_at is not None and last_mv is not None and last_mv > ov_at:
            case.semaforo_override = None
            case.semaforo_override_at = None
            case.semaforo_override_by = None
            return computed
        return override

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
        if getattr(trigger, "movement_date", None) is None:
            raise ValueError(f"Trigger has no movement_date: {trigger!r}")
        # Anchors to the "Diligencia:" date when present (see anchor_date).
        return anchor_date(trigger)

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

    @classmethod
    def _compute_apremio_substage(cls, db: Session, case: "Case") -> Optional[str]:
        """Detect the apremio SUB-stage — currently only ``"remate"``.

        Mirrors ``_compute_en_apremio`` exactly (same movement-loading via
        the loaded relationship or a query; same never-raises contract):

          Returns ``"remate"`` when ANY movement has stage OR description
          (case-insensitive) containing 'remate', 'martillero', or 'subasta'
          — i.e. the auction sub-stage of the cuaderno de apremio has begun.

          Returns ``None`` for a generic embargo-only apremio (or on any
          error). This is a RECOMMENDATION refinement only — it carries no
          deadline and is stored transiently on the case (``_apremio_substage``)
          for ``DecisionEngine`` to read; it is never persisted to a column.
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
                for token in ("remate", "martillero", "subasta"):
                    if token in stage or token in desc:
                        return "remate"
            return None
        except Exception:
            return None

    @classmethod
    def neutralize_medida_prejudicial(cls, db: Session, case: Case) -> None:
        """Make a 'Medida prejudicial' row neutral (flush-only, idempotent).

        Same state as ``_write_gris`` plus what it leaves alone (``en_apremio``
        and the manual semáforo override). Every NON-manual ``case_deadlines``
        row is marked ``superseded`` with ``verdict`` NULL in ONE statement;
        manual rows and the rows themselves (history) are kept.
        """
        case.en_apremio = False
        case._apremio_substage = None
        case.semaforo_override = None
        case.semaforo_override_at = None
        case.semaforo_override_by = None
        cls._write_gris(case, db)
        (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.is_manual == False,  # noqa: E712
                or_(
                    CaseDeadline.status != "superseded",
                    CaseDeadline.verdict.isnot(None),
                ),
            )
            .update(
                {CaseDeadline.status: "superseded", CaseDeadline.verdict: None},
                synchronize_session="evaluate",
            )
        )

    @staticmethod
    def _write_gris(case: Case, db: Session) -> None:
        """Set case to GRIS/INDETERMINATE and flush (safe-fail path)."""
        case.procedural_state = ProceduralState.INDETERMINATE.value
        case.semaforo = "gris"
        case.next_deadline_at = None
        case.abandono_disponible = False
        case.next_deadline_fatal = False
        case.prescripcion_cumplida = False
        case.plazo_fatal_incumplido = False
        case.prescripcion_fecha = None
        case.recommended_action_code = None
        case.next_review_at = None
        db.flush()
