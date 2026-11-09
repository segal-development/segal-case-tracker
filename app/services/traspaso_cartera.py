"""Traspaso de cartera — apply a plan, keep exact history, revert a batch.

Every write goes through the ORM (never ``query.update``): the 60 s case-scope
cache in ``lawyer_roster`` is invalidated by the ORM flush/commit events that fire
on ``Case.assigned_lawyer_id`` changes, and bulk updates bypass them.

History: one ``CaseAssignmentHistory`` row per case per batch stores the previous
override columns exactly as they were (NULL included) plus the resolved owner
before the move, so ``revert_batch`` restores the previous state exactly.
``Case.lawyer_id`` (sync provenance) is never touched.
"""

import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.case_assignment_history import CaseAssignmentHistory
from app.models.lawyer import Lawyer
from app.services.traspaso_plan import is_eligible_destination, owned_cases


class TraspasoError(Exception):
    """A request the apply/revert cannot honour; ``status`` is the HTTP code."""

    def __init__(self, status: int, detail: Any):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def motivo_traspaso(origen: Lawyer) -> str:
    return f"Traspaso de cartera de {origen.name}"


def record_history(
    db: Session,
    case: Case,
    *,
    batch_id: str,
    from_lawyer_id: Optional[int],
    to_lawyer_id: int,
    motivo: Optional[str],
    actor_rut: Optional[str],
    now: datetime,
) -> CaseAssignmentHistory:
    """Snapshot the case's CURRENT override columns; call BEFORE overwriting them."""
    row = CaseAssignmentHistory(
        batch_id=batch_id,
        case_id=case.id,
        from_lawyer_id=from_lawyer_id,
        prev_assigned_lawyer_id=case.assigned_lawyer_id,
        prev_assigned_at=case.assigned_at,
        prev_assigned_by_rut=case.assigned_by_rut,
        prev_assigned_motivo=case.assigned_motivo,
        to_lawyer_id=to_lawyer_id,
        motivo=motivo,
        created_by_rut=actor_rut,
        created_at=now,
    )
    db.add(row)
    return row


def write_override(case: Case, lawyer_id: int, actor_rut: str, motivo: Optional[str], now: datetime) -> None:
    case.assigned_lawyer_id = lawyer_id
    case.assigned_at = now
    case.assigned_by_rut = actor_rut
    case.assigned_motivo = motivo


def apply_traspaso(
    db: Session,
    origen_id: int,
    pairs: Iterable[Tuple[int, int]],
    *,
    actor_rut: str,
    bloquear_origen: bool = False,
) -> Dict[str, Any]:
    """Move ``pairs`` (case_id, destino_lawyer_id) off the origin in ONE transaction."""
    pairs = list(pairs)
    origen = db.get(Lawyer, origen_id)
    if origen is None:
        raise TraspasoError(422, "El abogado de origen no existe")

    case_ids = [cid for cid, _ in pairs]
    if len(set(case_ids)) != len(case_ids):
        raise TraspasoError(422, "Hay causas repetidas en las asignaciones")

    dest_ids = {did for _, did in pairs}
    destinos = {lw.id: lw for lw in db.query(Lawyer).filter(Lawyer.id.in_(dest_ids)).all()} if dest_ids else {}
    for did in sorted(dest_ids):
        if did == origen.id or not is_eligible_destination(destinos.get(did)):
            raise TraspasoError(422, f"El destino {did} no es un abogado activo de la firma distinto del origen")

    cases = {c.id: c for c in db.query(Case).filter(Case.id.in_(case_ids)).all()} if case_ids else {}
    missing = [cid for cid in case_ids if cid not in cases]
    if missing:
        raise TraspasoError(422, f"Causas inexistentes: {missing}")

    owned = {c.id for c in owned_cases(db, origen.id)}
    changed = [cid for cid in case_ids if cid not in owned]
    if changed:
        raise TraspasoError(409, {"message": "Causas que ya no pertenecen al origen", "case_ids": changed})

    batch_id = str(uuid.uuid4())
    now = datetime.utcnow()
    motivo = motivo_traspaso(origen)
    try:
        for case_id, dest_id in pairs:
            case = cases[case_id]
            record_history(
                db, case, batch_id=batch_id, from_lawyer_id=origen.id, to_lawyer_id=dest_id,
                motivo=motivo, actor_rut=actor_rut, now=now,
            )
            write_override(case, dest_id, actor_rut, motivo, now)
        if bloquear_origen:
            from app.api.v1.lawyers import block_account

            block_account(origen)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"batch_id": batch_id, "aplicadas": len(pairs), "bloqueada": bool(bloquear_origen)}


def revert_batch(db: Session, batch_id: str) -> Dict[str, Any]:
    """Restore each case's previous override columns exactly; skip changed cases."""
    rows: List[CaseAssignmentHistory] = (
        db.query(CaseAssignmentHistory)
        .filter(CaseAssignmentHistory.batch_id == batch_id)
        .order_by(CaseAssignmentHistory.id)
        .all()
    )
    if not rows:
        raise TraspasoError(404, "Lote no encontrado")
    pending = [r for r in rows if r.reverted_at is None]
    if not pending:
        raise TraspasoError(409, "El lote ya fue revertido")

    now = datetime.utcnow()
    omitidas = []
    revertidas = 0
    try:
        for row in pending:
            case = db.get(Case, row.case_id)
            if (
                case is None
                or case.assigned_lawyer_id != row.to_lawyer_id
                or case.assigned_motivo != row.motivo
            ):
                omitidas.append(
                    {"case_id": row.case_id, "motivo": "La asignación cambió después del traspaso"}
                )
                continue
            case.assigned_lawyer_id = row.prev_assigned_lawyer_id
            case.assigned_at = row.prev_assigned_at
            case.assigned_by_rut = row.prev_assigned_by_rut
            case.assigned_motivo = row.prev_assigned_motivo
            row.reverted_at = now
            revertidas += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"batch_id": batch_id, "revertidas": revertidas, "omitidas": omitidas}


def list_batches(db: Session) -> List[Dict[str, Any]]:
    rows = db.query(CaseAssignmentHistory).order_by(CaseAssignmentHistory.id).all()
    batches: Dict[str, List[CaseAssignmentHistory]] = {}
    for row in rows:
        batches.setdefault(row.batch_id, []).append(row)
    names = {lw.id: lw.name for lw in db.query(Lawyer).all()}
    out = []
    for batch_id, items in batches.items():
        first = items[0]
        out.append(
            {
                "batch_id": batch_id,
                "created_at": min(r.created_at for r in items),
                "created_by": first.created_by_rut,
                "origen_lawyer_id": first.from_lawyer_id,
                "origen_nombre": names.get(first.from_lawyer_id),
                "count": len(items),
                "reverted": all(r.reverted_at is not None for r in items),
            }
        )
    out.sort(key=lambda b: b["created_at"], reverse=True)
    return out
