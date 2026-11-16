"""Procurador accounts: the 1-2 firm lawyers each one supports (links, validation, lookups)."""

from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.lawyer import Lawyer
from app.models.procurador import ProcuradorAbogado

ROLE = "procurador"
MSG_LINKS = "Un procurador debe apoyar a 1 o 2 abogados."
MSG_ABOGADO_INVALIDO = "Los abogados que apoya deben ser abogados activos del estudio."
MSG_NOT_SUPPORTED = "Solo puedes ingresar hitos de los abogados que apoyas."


def is_procurador(lawyer: Optional[Lawyer]) -> bool:
    return bool(lawyer is not None and lawyer.role == ROLE)


def linked_abogados(db: Session, procurador_id: int) -> list[Lawyer]:
    """The ACTIVE firm lawyers this procurador supports, ordered by name."""
    return (
        db.query(Lawyer)
        .join(ProcuradorAbogado, ProcuradorAbogado.abogado_id == Lawyer.id)
        .filter(
            ProcuradorAbogado.procurador_id == procurador_id,
            Lawyer.is_active.isnot(False),
            Lawyer.is_firm_lawyer.is_(True),
        )
        .order_by(Lawyer.name, Lawyer.id)
        .all()
    )


def linked_abogado_ids(db: Session, procurador_id: int) -> set[int]:
    return {lw.id for lw in linked_abogados(db, procurador_id)}


def abogados_by_procurador(db: Session, procurador_ids: Iterable[int]) -> dict[int, list[Lawyer]]:
    """Batch lookup for the accounts list (one query, no N+1)."""
    ids = list(procurador_ids)
    out: dict[int, list[Lawyer]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = (
        db.query(ProcuradorAbogado.procurador_id, Lawyer)
        .join(Lawyer, Lawyer.id == ProcuradorAbogado.abogado_id)
        .filter(ProcuradorAbogado.procurador_id.in_(ids))
        .order_by(Lawyer.name, Lawyer.id)
        .all()
    )
    for pid, lw in rows:
        out[pid].append(lw)
    return out


def validate_abogado_ids(db: Session, abogado_ids: Optional[Iterable[int]]) -> list[Lawyer]:
    """1 or 2 distinct ACTIVE firm lawyers, else 422 with the exact product messages."""
    ids = list(dict.fromkeys(abogado_ids or []))
    if len(ids) < 1 or len(ids) > 2:
        raise HTTPException(status_code=422, detail=MSG_LINKS)
    found = (
        db.query(Lawyer)
        .filter(Lawyer.id.in_(ids), Lawyer.is_active.isnot(False), Lawyer.is_firm_lawyer.is_(True))
        .all()
    )
    if len(found) != len(ids):
        raise HTTPException(status_code=422, detail=MSG_ABOGADO_INVALIDO)
    return found


def set_links(db: Session, procurador: Lawyer, abogados: list[Lawyer]) -> None:
    """Replace the procurador's links (ORM deletes/inserts so the scope cache is invalidated)."""
    wanted = {lw.id for lw in abogados}
    current = db.query(ProcuradorAbogado).filter(ProcuradorAbogado.procurador_id == procurador.id).all()
    for row in current:
        if row.abogado_id not in wanted:
            db.delete(row)
    have = {row.abogado_id for row in current}
    for ab_id in wanted - have:
        db.add(ProcuradorAbogado(procurador_id=procurador.id, abogado_id=ab_id))
    db.flush()


def clear_links(db: Session, procurador: Lawyer) -> None:
    for row in db.query(ProcuradorAbogado).filter(ProcuradorAbogado.procurador_id == procurador.id).all():
        db.delete(row)
    db.flush()
