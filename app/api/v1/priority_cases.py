"""Causas prioritarias: each lawyer's personal marks (max 20)."""

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import apply_case_scope, get_current_lawyer, get_db, resolve_case_scope
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_priority import CasePriority
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement

router = APIRouter()

MAX_PRIORITY_CASES = 20
CAP_DETAIL = f"Máximo {MAX_PRIORITY_CASES} causas prioritarias por abogado."


class UltimoMovimiento(BaseModel):
    descripcion: str
    fecha: date


class PriorityCaseItem(BaseModel):
    case_id: int
    rol: str
    caratula: str
    tribunal: Optional[str] = None
    semaforo: Optional[str] = None
    last_detail_checked_at: Optional[datetime] = None
    marcada_at: datetime
    ultimo_movimiento: Optional[UltimoMovimiento] = None
    movimiento_nuevo: bool = False


class PriorityCaseListResponse(BaseModel):
    items: List[PriorityCaseItem]
    total: int
    max: int = MAX_PRIORITY_CASES
    con_movimiento_nuevo: int = 0
    ultima_revision: Optional[datetime] = None


class PriorityBulkRequest(BaseModel):
    case_ids: List[int]
    on: bool


class PriorityBulkResponse(BaseModel):
    total: int


def own_lawyer_id(db: Session, current_lawyer: dict) -> int:
    """The signed-in person's own ``lawyers.id`` (marks are personal, so no auditor→firm remap)."""
    sub = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token")
    if isinstance(sub, int) or (isinstance(sub, str) and sub.isdigit()):
        row = db.query(Lawyer.id).filter(Lawyer.id == int(sub)).first()
    else:
        row = db.query(Lawyer.id).filter(Lawyer.rut == sub).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Lawyer not found")
    return int(row[0])


def priority_case_ids(db: Session, lawyer_id: int, case_ids) -> set:
    """Which of ``case_ids`` the lawyer marked (ONE query)."""
    ids = list(case_ids)
    if not ids:
        return set()
    rows = (
        db.query(CasePriority.case_id)
        .filter(CasePriority.lawyer_id == lawyer_id, CasePriority.case_id.in_(ids))
        .all()
    )
    return {r[0] for r in rows}


def unread_movement_alert_clause(lawyer_id: int):
    """Alert rows that make a causa "movimiento nuevo" for ``lawyer_id``."""
    return (
        Alert.lawyer_id == lawyer_id,
        Alert.movement_id.isnot(None),
        # Same 'unread' as Novedades (alerts.py, alert_groups.py): the boolean,
        # not read_at, which older rows may lack even when read.
        Alert.read.is_(False),
    )


def movimiento_nuevo_case_ids(db: Session, lawyer_id: int, case_ids) -> set:
    """Which of ``case_ids`` have an unread movement alert for the lawyer (ONE query)."""
    ids = list(case_ids)
    if not ids:
        return set()
    rows = (
        db.query(Alert.case_id)
        .filter(*unread_movement_alert_clause(lawyer_id), Alert.case_id.in_(ids))
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def _ultimo_movimiento_by_case(db: Session, case_ids) -> dict:
    """Latest movement (description, date) per case (ONE query)."""
    ids = list(case_ids)
    if not ids:
        return {}
    rank = func.row_number().over(
        partition_by=Movement.case_id,
        order_by=(Movement.movement_date.desc(), Movement.id.desc()),
    ).label("rn")
    sub = (
        db.query(Movement.case_id.label("case_id"), Movement.description.label("d"),
                 Movement.movement_date.label("f"), rank)
        .filter(Movement.case_id.in_(ids))
        .subquery()
    )
    rows = db.query(sub.c.case_id, sub.c.d, sub.c.f).filter(sub.c.rn == 1).all()
    return {cid: (d, f) for cid, d, f in rows}


_SEMAFORO_RANK = {"rojo": 0, "amarillo": 1, "verde": 2}


def _scoped_existing_ids(db: Session, current_lawyer: dict, ids: set) -> set:
    scope = resolve_case_scope(db, current_lawyer)
    rows = apply_case_scope(db.query(Case.id).filter(Case.id.in_(ids)), scope).all()
    return {r[0] for r in rows}


def _count(db: Session, lawyer_id: int) -> int:
    return db.query(CasePriority).filter(CasePriority.lawyer_id == lawyer_id).count()


@router.get("/priority-cases", response_model=PriorityCaseListResponse)
async def list_priority_cases(
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    lawyer_id = own_lawyer_id(db, current_lawyer)
    scope = resolve_case_scope(db, current_lawyer)
    query = (
        db.query(Case, Court.name, CasePriority.created_at)
        .join(CasePriority, CasePriority.case_id == Case.id)
        .outerjoin(Court, Court.id == Case.court_id)
        .filter(CasePriority.lawyer_id == lawyer_id)
    )
    rows = (
        apply_case_scope(query, scope)
        .order_by(CasePriority.created_at.desc(), CasePriority.id.desc())
        .all()
    )
    rows = list(rows)
    case_ids = [c.id for c, _, _ in rows]
    ultimos = _ultimo_movimiento_by_case(db, case_ids)
    nuevos = movimiento_nuevo_case_ids(db, lawyer_id, case_ids)
    items = []
    for c, court_name, marcada in rows:
        last = ultimos.get(c.id)
        items.append(
            PriorityCaseItem(
                case_id=c.id,
                rol=c.rol,
                caratula=f"{c.plaintiff or ''}/{c.defendant or ''}",
                tribunal=court_name,
                semaforo=c.semaforo,
                last_detail_checked_at=c.last_detail_checked_at,
                marcada_at=marcada,
                ultimo_movimiento=(
                    UltimoMovimiento(descripcion=last[0] or "", fecha=last[1].date()) if last else None
                ),
                movimiento_nuevo=c.id in nuevos,
            )
        )
    # semáforo rank, then latest movement date desc (nulls last), then marcada_at desc.
    # Stable sorts, least significant key first.
    items.sort(key=lambda i: i.marcada_at, reverse=True)
    items.sort(key=lambda i: i.ultimo_movimiento.fecha if i.ultimo_movimiento else date.min, reverse=True)
    items.sort(key=lambda i: _SEMAFORO_RANK.get(i.semaforo, 3))
    checked = [i.last_detail_checked_at for i in items if i.last_detail_checked_at]
    return PriorityCaseListResponse(
        items=items,
        total=len(items),
        max=MAX_PRIORITY_CASES,
        con_movimiento_nuevo=sum(1 for i in items if i.movimiento_nuevo),
        ultima_revision=max(checked) if checked else None,
    )


@router.put("/cases/{case_id}/priority", status_code=status.HTTP_204_NO_CONTENT)
async def mark_priority(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    lawyer_id = own_lawyer_id(db, current_lawyer)
    if not _scoped_existing_ids(db, current_lawyer, {case_id}):
        raise HTTPException(status_code=404, detail="Case not found")
    if not priority_case_ids(db, lawyer_id, [case_id]):
        if _count(db, lawyer_id) >= MAX_PRIORITY_CASES:
            raise HTTPException(status_code=409, detail=CAP_DETAIL)
        db.add(CasePriority(case_id=case_id, lawyer_id=lawyer_id))
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/cases/{case_id}/priority", status_code=status.HTTP_204_NO_CONTENT)
async def unmark_priority(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    lawyer_id = own_lawyer_id(db, current_lawyer)
    db.query(CasePriority).filter(
        CasePriority.lawyer_id == lawyer_id, CasePriority.case_id == case_id
    ).delete(synchronize_session=False)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/priority-cases/bulk", response_model=PriorityBulkResponse)
async def bulk_priority(
    body: PriorityBulkRequest,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    lawyer_id = own_lawyer_id(db, current_lawyer)
    ids = set(body.case_ids)
    if ids and _scoped_existing_ids(db, current_lawyer, ids) != ids:
        raise HTTPException(status_code=404, detail="Case not found")
    marked = priority_case_ids(db, lawyer_id, ids)
    if body.on:
        new = ids - marked
        if _count(db, lawyer_id) + len(new) > MAX_PRIORITY_CASES:
            raise HTTPException(status_code=409, detail=CAP_DETAIL)
        for cid in sorted(new):
            db.add(CasePriority(case_id=cid, lawyer_id=lawyer_id))
    elif marked:
        db.query(CasePriority).filter(
            CasePriority.lawyer_id == lawyer_id, CasePriority.case_id.in_(marked)
        ).delete(synchronize_session=False)
    db.commit()
    return PriorityBulkResponse(total=_count(db, lawyer_id))
