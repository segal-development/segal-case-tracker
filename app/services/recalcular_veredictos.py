"""Re-evaluate the persisted verdict of CLOSED plazo rows with the current rule.

WHY IT EXISTS
``CaseDeadline.verdict`` is written once, when a plazo closes (by the engine or
by a backfill). When the rule in ``deadline_verdict.evaluate_deadline`` changes
afterwards (e.g. the publication margin), those rows keep the old answer. This
job re-runs ``evaluate_deadline`` over them. It does NOT encode any rule: it
calls the same function the engine and the Sysgal endpoint call, so whatever
moves the rule next time, this job still works unchanged.

WHICH ROWS (and why)
* ``status`` in (superseded, expired) AND verdict set: the plazo is closed. The
  engine only rewrites ACTIVE rows (and revives a closed one only if its
  trigger recurs); a live row gets its verdict from the engine at its next
  recompute, so touching it here would step on the engine.
* NEVER an auditor's decision: ``is_manual``, ``marked_by`` / ``marked_at`` set,
  or ``status`` cumplido / no_cumplido. The engine skips the same rows
  (``_record_verdict``). They are counted apart (``protegidas``), with the
  transition they WOULD have had, so a human can review them.
* A closed row WITHOUT verdict is left alone: superseded-without-verdict means
  "re-anchored, stale due_date" (see the Sysgal endpoint).

WHAT IT WRITES
Only the four ``verdict*`` columns of ``case_deadlines``. Never ``status``,
never ``Case`` columns, never ``recompute_case``, never the alert stack. The
semaforo and ``next_deadline_*`` read ``status`` (``_compute_semaforo``), not
the verdict, so changing a verdict cannot repaint anything.

MOVEMENTS ARE LOADED (deliberately)
``evaluate_deadline`` needs the movements because the detector of the act is the
part of the rule most likely to change next. Re-evaluating from the persisted
``verdict_acted_on`` would freeze that part. The cost is bounded: one query per
chunk of cases, only the columns the detectors read, only cases that own a
candidate row.

REVERSIBLE WITHOUT A BACKUP
Re-evaluating is a pure function of (rule, due_date, movements); no information
is lost. ``publication_margin_days=0`` reproduces the pre-margin rule, so
running with it restores the old verdicts, and running again without it
restores the new ones.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, load_only, selectinload

from app.models.case_deadline import CaseDeadline
from app.models.movement import Movement
from app.services.deadline_engine import DeadlineEngine, _today_chile
from app.services.deadline_verdict import evaluate_deadline

logger = logging.getLogger(__name__)

#: Lifecycle statuses of a closed plazo. ``active`` is the engine's.
CLOSED_STATUSES = ("superseded", "expired")
#: Statuses an auditor sets by hand.
AUDITOR_STATUSES = ("cumplido", "no_cumplido")

_COMMIT_CHUNK = 200
_LOAD_CHUNK = 500

Transicion = Tuple[str, str]  # (verdict before, verdict after)


@dataclass(frozen=True)
class Cambio:
    deadline_id: int
    case_id: int
    antes: str
    despues: str
    movement_id: Optional[int]
    acted_on: Optional[date]


@dataclass
class Resultado:
    apply: bool
    cambios: List[Cambio] = field(default_factory=list)
    evaluadas: int = 0
    protegidas: int = 0  # auditor-marked rows with a verdict (never touched)
    protegidas_que_cambiarian: Dict[Transicion, int] = field(default_factory=dict)
    vigentes: int = 0  # active rows with a verdict (the engine's, never touched)
    sin_veredicto_nuevo: int = 0  # rule now says "sin determinar": left as is

    @property
    def transiciones(self) -> Dict[Transicion, int]:
        return dict(Counter((c.antes, c.despues) for c in self.cambios))


def _is_protected(row: CaseDeadline) -> bool:
    return (
        row.is_manual
        or row.marked_by is not None
        or row.marked_at is not None
        or row.status in AUDITOR_STATUSES
    )


def _load_movements(db: Session, case_ids: List[int]) -> Dict[int, List[Movement]]:
    by_case: Dict[int, List[Movement]] = {}
    query = (
        db.query(Movement)
        .options(load_only(
            Movement.id, Movement.case_id, Movement.movement_date,
            Movement.description, Movement.procedure,
        ))
        .filter(Movement.case_id.in_(case_ids))
        .order_by(Movement.movement_date, Movement.id)
    )
    for mv in query:
        by_case.setdefault(mv.case_id, []).append(mv)
    return by_case


def planificar(
    db: Session,
    *,
    today: Optional[date] = None,
    publication_margin_days: Optional[int] = None,
) -> Resultado:
    """Read-only: evaluate every candidate row and list the ones that change."""
    today = today or _today_chile()
    resultado = Resultado(apply=False)

    resultado.vigentes = (
        db.query(CaseDeadline)
        .filter(CaseDeadline.status == "active", CaseDeadline.verdict.isnot(None))
        .count()
    )

    rows = (
        db.query(CaseDeadline)
        .options(selectinload(CaseDeadline.source_movement))
        .filter(CaseDeadline.status != "active", CaseDeadline.verdict.isnot(None))
        .order_by(CaseDeadline.case_id, CaseDeadline.id)
        .all()
    )
    # Closed by us means closed status; any other non-active, non-auditor status
    # (e.g. "met") is not ours to judge.
    candidates = [r for r in rows if _is_protected(r) or r.status in CLOSED_STATUSES]

    protegidas_cambian: Counter = Counter()
    for start in range(0, len(candidates), _LOAD_CHUNK):
        chunk = candidates[start:start + _LOAD_CHUNK]
        movements = _load_movements(db, sorted({r.case_id for r in chunk}))
        for row in chunk:
            protegida = _is_protected(row)
            if protegida:
                resultado.protegidas += 1
            else:
                resultado.evaluadas += 1
            result = evaluate_deadline(
                row.deadline_type,
                DeadlineEngine._current_due_date(row),
                movements.get(row.case_id, []),
                today,
                publication_margin_days=publication_margin_days,
            )
            if result.verdict is None:
                if not protegida:
                    resultado.sin_veredicto_nuevo += 1
                continue
            same = (
                result.verdict.value == row.verdict
                and result.movement_id == row.verdict_movement_id
                and result.acted_on == row.verdict_acted_on
            )
            if same:
                continue
            if protegida:
                if result.verdict.value != row.verdict:
                    protegidas_cambian[(row.verdict, result.verdict.value)] += 1
                continue
            resultado.cambios.append(Cambio(
                deadline_id=row.id, case_id=row.case_id, antes=row.verdict,
                despues=result.verdict.value, movement_id=result.movement_id,
                acted_on=result.acted_on,
            ))
    resultado.protegidas_que_cambiarian = dict(protegidas_cambian)
    return resultado


def aplicar(db: Session, cambios: List[Cambio]) -> None:
    """Write the verdict columns of the planned rows. Touches nothing else."""
    now = datetime.now(timezone.utc)
    for i in range(0, len(cambios), _COMMIT_CHUNK):
        chunk = cambios[i:i + _COMMIT_CHUNK]
        rows = {
            r.id: r
            for r in db.query(CaseDeadline).filter(
                CaseDeadline.id.in_([c.deadline_id for c in chunk])
            )
        }
        for c in chunk:
            row = rows[c.deadline_id]
            row.verdict = c.despues
            row.verdict_movement_id = c.movement_id
            row.verdict_acted_on = c.acted_on
            row.verdict_computed_at = now
        db.commit()


def ejecutar(
    db: Session,
    *,
    apply: bool = False,
    today: Optional[date] = None,
    publication_margin_days: Optional[int] = None,
) -> Resultado:
    """Plan and, only with ``apply=True``, write. Dry-run is the default."""
    resultado = planificar(
        db, today=today, publication_margin_days=publication_margin_days
    )
    resultado.apply = apply
    if apply and resultado.cambios:
        aplicar(db, resultado.cambios)
        logger.info("recalcular veredictos: %d rows updated", len(resultado.cambios))
    return resultado
