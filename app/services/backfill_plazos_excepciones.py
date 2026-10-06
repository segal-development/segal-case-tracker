"""Backfill of the missing ``excepciones_8d`` plazo rows.

WHY IT EXISTS
The engine creates the ``excepciones_8d`` row only if a recompute ran while the
case was NOTIFICADO. A case first scraped AFTER the debtor filed its excepciones
never got one, so ``GET /api/sysgal/v1/excepciones`` can only answer
``presentado_sin_ancla`` (filed, but no plazo to measure it against). This job
CREATES those rows from the movements. It does not recompute verdicts.

WHAT MAKES IT SAFE (the whole point)
Creating thousands of fatal, long-expired plazos must be RECORDED, never
ANNOUNCED, and must not repaint the portfolio red. So this module:

* never calls ``DeadlineEngine.recompute_case`` and never imports the alert
  stack: alerts fire only from ``sync_service.emit_deadline_alerts`` off a
  ``SemaforoTransition`` returned by ``recompute_case``. No recompute, no
  transition, no alert;
* never writes ``Case`` columns (``semaforo``, ``next_deadline_at``,
  ``next_deadline_fatal``);
* writes every row with ``status="superseded"``. The semaforo and
  ``next_deadline_at`` read only ``status == "active"`` rows, and the only path
  to ROJO from a closed row is ``status == "expired"`` on a mandatory type
  (``DeadlineEngine._compute_semaforo``) — so ``expired`` is forbidden here.
  ``superseded`` is also what the engine itself does with a plazo once the case
  moves on (``_record_verdict`` then ``superseded``), and it keeps the row out of
  the calendar and the case's plazo lists, while the Sysgal endpoint still shows
  it because its verdict is set.

REVERSIBLE: every row carries ``origin = ORIGIN`` (migration 064); ``revertir``
deletes the rows still bearing it that nobody has touched since.
IDEMPOTENT: a case that already has ANY ``excepciones_8d`` row is not a candidate.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.core.deadlines_config import DeadlineType
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.movement import Movement
from app.services.business_days import add_business_days
from app.services.deadline_engine import _today_chile
from app.services.deadline_verdict import evaluate_deadline, is_excepciones_filing
from app.services.procedural_classifier import MovementClassifier, anchor_date

logger = logging.getLogger(__name__)

#: Provenance stamp written to ``CaseDeadline.origin``. Fits String(40).
ORIGIN = "backfill_excepciones_2026_10"

_TYPE = DeadlineType.EXCEPCIONES_8D
_CLASSIFIER = MovementClassifier()

#: Status of the created rows. NEVER "active" (would feed the semaforo and the
#: calendar) and NEVER "expired" (a mandatory expired row turns the case ROJO).
CLOSED_STATUS = "superseded"

_COMMIT_CHUNK = 200
_LOAD_CHUNK = 500


class Alcance(str, Enum):
    ACTIVAS = "activas"
    TODAS = "todas"


@dataclass(frozen=True)
class FilaPlanificada:
    case_id: int
    case_status: str
    procedural_state: Optional[str]
    triggered_at: date
    due_date: date
    source_movement_id: Optional[int]
    verdict: str
    verdict_movement_id: Optional[int]
    verdict_acted_on: Optional[date]


@dataclass
class Plan:
    """Everything the job found, over ALL cases (scope is applied at apply time)."""

    filas: List[FilaPlanificada] = field(default_factory=list)
    sin_ancla: List[tuple] = field(default_factory=list)  # (case_id, status)
    no_civil: int = 0
    plazo_vigente_en_motor: int = 0
    # Cases whose only excepciones row is a superseded one WITHOUT verdict: the
    # endpoint still answers sin_ancla for them. Reported, never touched.
    con_fila_obsoleta_sin_veredicto: int = 0

    @property
    def a_crear_total(self) -> int:
        return len(self.filas)

    @property
    def sin_ancla_total(self) -> int:
        return len(self.sin_ancla)

    @property
    def con_presentacion_sin_fila(self) -> int:
        return self.a_crear_total + self.sin_ancla_total + self.no_civil + self.plazo_vigente_en_motor

    @property
    def a_crear_por_estado(self) -> Dict[str, int]:
        return dict(Counter(f.case_status for f in self.filas))

    @property
    def a_crear_por_veredicto(self) -> Dict[str, int]:
        return dict(Counter(f.verdict for f in self.filas))

    @property
    def a_crear_por_estado_y_veredicto(self) -> Dict[tuple, int]:
        return dict(Counter((f.case_status, f.verdict) for f in self.filas))

    @property
    def a_crear_por_estado_procesal(self) -> Dict[str, int]:
        return dict(Counter(f.procedural_state or "—" for f in self.filas))

    @property
    def sin_ancla_por_estado(self) -> Dict[str, int]:
        return dict(Counter(status for _, status in self.sin_ancla))


@dataclass
class Resultado:
    plan: Plan
    alcance: Alcance
    apply: bool
    creadas: List[int] = field(default_factory=list)  # case_deadlines ids

    @property
    def en_alcance(self) -> int:
        return len(_en_alcance(self.plan.filas, self.alcance))


@dataclass
class ResultadoReversion:
    eliminadas: int = 0
    conservadas: int = 0


def _is_active(case_status: Optional[str]) -> bool:
    # Case.status defaults to "active"; NULL is treated as active.
    return case_status in (None, "active")


def _en_alcance(filas: Sequence[FilaPlanificada], alcance: Alcance) -> List[FilaPlanificada]:
    if alcance == Alcance.TODAS:
        return list(filas)
    return [f for f in filas if _is_active(f.case_status)]


def _find_anchor_movement(movements: List[Movement], today: date) -> Optional[object]:
    """The movement that started the plazo, replaying the engine's classifier.

    The engine's own classification of the FULL history no longer contains the
    plazo once the case advanced (state advance clears the triggers), so replay
    growing prefixes and stop at the first one that starts it. Reuses the real
    classifier: no second copy of the rule.
    """
    for i in range(1, len(movements) + 1):
        _, triggers = _CLASSIFIER.classify(movements[:i], today)
        trigger = triggers.get(_TYPE)
        if trigger is not None:
            return trigger
    return None


def planificar(db: Session, *, today: Optional[date] = None) -> Plan:
    """Read-only: find cases with a filing and no plazo row and plan their rows."""
    today = today or _today_chile()
    plan = Plan()

    candidate_ids = [
        cid
        for (cid,) in db.query(Movement.case_id)
        .filter(Movement.description.ilike("%opone%excepciones%"))
        .distinct()
        .order_by(Movement.case_id)
        .all()
    ]

    for start in range(0, len(candidate_ids), _LOAD_CHUNK):
        ids = candidate_ids[start:start + _LOAD_CHUNK]
        cases = {c.id: c for c in db.query(Case).filter(Case.id.in_(ids)).all()}

        rows_by_case: Dict[int, List[CaseDeadline]] = {}
        for row in db.query(CaseDeadline).filter(
            CaseDeadline.case_id.in_(ids), CaseDeadline.deadline_type == _TYPE.value
        ):
            rows_by_case.setdefault(row.case_id, []).append(row)

        movements_by_case: Dict[int, List[Movement]] = {}
        for mv in (
            db.query(Movement)
            .filter(Movement.case_id.in_(ids))
            .order_by(Movement.movement_date, Movement.id)
        ):
            movements_by_case.setdefault(mv.case_id, []).append(mv)

        for cid in ids:
            case = cases.get(cid)
            movements = movements_by_case.get(cid, [])
            if case is None or not any(is_excepciones_filing(m) for m in movements):
                continue  # the SQL prefilter is a superset; the real filter is here

            existing = rows_by_case.get(cid, [])
            if existing:
                if all(r.status == "superseded" and r.verdict is None for r in existing):
                    plan.con_fila_obsoleta_sin_veredicto += 1
                continue

            if (case.competencia or "civil").lower() != "civil":
                plan.no_civil += 1
                continue

            # Still running for the engine: its next recompute creates the live
            # row itself. A closed row here would misstate an open plazo.
            _, final_triggers = _CLASSIFIER.classify(movements, today)
            if _TYPE in final_triggers:
                plan.plazo_vigente_en_motor += 1
                continue

            trigger = _find_anchor_movement(movements, today)
            if trigger is None:
                # No notification to count from: never invent an anchor.
                plan.sin_ancla.append((cid, case.status))
                continue

            triggered_at = anchor_date(trigger)
            due_date = add_business_days(triggered_at, _TYPE.dias_habiles)
            result = evaluate_deadline(_TYPE.value, due_date, movements, today)
            if result.verdict is None:  # unreachable: a filing exists
                plan.sin_ancla.append((cid, case.status))
                continue

            plan.filas.append(FilaPlanificada(
                case_id=cid,
                case_status=case.status or "active",
                procedural_state=case.procedural_state,
                triggered_at=triggered_at,
                due_date=due_date,
                source_movement_id=getattr(trigger, "id", None),
                verdict=result.verdict.value,
                verdict_movement_id=result.movement_id,
                verdict_acted_on=result.acted_on,
            ))

    return plan


def aplicar(db: Session, filas: Sequence[FilaPlanificada]) -> List[int]:
    """Insert the planned rows, silently. Commits per chunk; returns new ids.

    Touches ONLY ``case_deadlines``: no recompute, no ``Case`` write, no alert.
    """
    creadas: List[int] = []
    now = datetime.now(timezone.utc)
    for i in range(0, len(filas), _COMMIT_CHUNK):
        batch = []
        for f in filas[i:i + _COMMIT_CHUNK]:
            row = CaseDeadline(
                case_id=f.case_id,
                deadline_type=_TYPE.value,
                legal_basis=_TYPE.legal_basis,
                due_date=f.due_date,
                triggered_at=f.triggered_at,
                status=CLOSED_STATUS,
                source_movement_id=f.source_movement_id,
                computed_at=now,
                verdict=f.verdict,
                verdict_movement_id=f.verdict_movement_id,
                verdict_acted_on=f.verdict_acted_on,
                verdict_computed_at=now,
                origin=ORIGIN,
            )
            db.add(row)
            batch.append(row)
        db.flush()
        creadas.extend(r.id for r in batch)
        db.commit()
    return creadas


def ejecutar(
    db: Session,
    *,
    apply: bool = False,
    alcance: Alcance = Alcance.ACTIVAS,
    today: Optional[date] = None,
) -> Resultado:
    """Plan and, only with ``apply=True``, write. Dry-run is the default."""
    plan = planificar(db, today=today)
    resultado = Resultado(plan=plan, alcance=alcance, apply=apply)
    if apply:
        resultado.creadas = aplicar(db, _en_alcance(plan.filas, alcance))
        logger.info("backfill excepciones: %d rows created (origin=%s)", len(resultado.creadas), ORIGIN)
    return resultado


def _is_untouched(row: CaseDeadline) -> bool:
    return (
        row.status == CLOSED_STATUS
        and not row.is_manual
        and row.marked_by is None
        and row.marked_at is None
    )


def contar_reversion(db: Session) -> ResultadoReversion:
    """Read-only preview of ``revertir``: what it would delete and what it would keep."""
    resultado = ResultadoReversion()
    for row in db.query(CaseDeadline).filter(CaseDeadline.origin == ORIGIN):
        if _is_untouched(row):
            resultado.eliminadas += 1
        else:
            resultado.conservadas += 1
    return resultado


def revertir(db: Session) -> ResultadoReversion:
    """Delete the rows this job created, unless someone touched them since.

    A row is deleted only if it is still exactly as created: ``origin`` is ours,
    status is still ``superseded``, and it is neither manual nor audited. A row
    the engine revived or an auditor marked is a decision made on top of ours:
    kept, and counted in ``conservadas``.
    """
    resultado = ResultadoReversion()
    for row in db.query(CaseDeadline).filter(CaseDeadline.origin == ORIGIN).all():
        if _is_untouched(row):
            db.delete(row)
            resultado.eliminadas += 1
        else:
            resultado.conservadas += 1
    db.commit()
    return resultado
