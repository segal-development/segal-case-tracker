"""Lawyers endpoint — firm roster and admin account management."""

from datetime import datetime, timedelta
from typing import Optional

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_current_lawyer, require_admin, _resolve_lawyer_id
from app.core.security import hash_password
from app.models.case import Case
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import firm_roster
from app.utils.rut import normalize_rut, validate_rut

_STALE_THRESHOLD_DAYS = 7

router = APIRouter()


class LawyerRosterItem(BaseModel):
    rut: str
    nombre: str
    case_count: int

    model_config = ConfigDict(from_attributes=True)


class AccountUpdateBody(BaseModel):
    """Request body for PUT /{id}/account — all fields optional."""
    email: Optional[str] = None
    role: Optional[str] = None
    new_password: Optional[str] = None
    # False blocks the person (sign-in and open sessions stop; nothing is deleted); True reactivates.
    is_active: Optional[bool] = None


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NIVELES = {"junior", "pleno"}


class LawyerCreateBody(BaseModel):
    """POST /lawyers: add a person to the firm by hand (they used to appear only via the PJUD sync)."""
    rut: str
    name: str
    email: Optional[str] = None
    nivel: Optional[str] = None

    @field_validator("rut")
    @classmethod
    def _rut(cls, v: str) -> str:
        if not validate_rut(v):
            raise ValueError("RUT inválido (revisa el dígito verificador)")
        return normalize_rut(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("El nombre es obligatorio")
        if len(v) > 255:
            raise ValueError("El nombre admite hasta 255 caracteres")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip().lower()
        if not _EMAIL.match(v) or len(v) > 255:
            raise ValueError("Correo inválido")
        return v

    @field_validator("nivel")
    @classmethod
    def _nivel(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _NIVELES:
            raise ValueError("El nivel debe ser 'junior' o 'pleno'")
        return v


class SyncStatusItem(BaseModel):
    """Per-lawyer civil-case sync status for the operator panel."""

    id: int
    rut: str
    name: str
    email: Optional[str]
    role: str
    case_count: int
    last_synced_at: Optional[datetime]
    stale_count: int

    model_config = ConfigDict(from_attributes=True)


@router.get("/sync-status", response_model=list[SyncStatusItem], dependencies=[Depends(require_admin)])
def get_sync_status(db: Session = Depends(get_db)):
    """Per-lawyer civil-case sync status for the operator panel (admin only).

    Returns one entry per lawyer who has at least one civil case, ordered by
    last_synced_at ASC, NULLs first (least-recently-synced lawyers appear first).

    - case_count:     total civil cases under this lawyer
    - last_synced_at: MAX(last_detail_checked_at) across all civil cases (None if all NULL)
    - stale_count:    cases where last_detail_checked_at IS NULL or older than 7 days
    """
    cutoff = datetime.utcnow() - timedelta(days=_STALE_THRESHOLD_DAYS)

    # Subquery: per-lawyer aggregates from civil cases only
    sub = (
        db.query(
            Case.lawyer_id,
            func.count(Case.id).label("case_count"),
            func.max(Case.last_detail_checked_at).label("last_synced_at"),
            func.sum(
                case(
                    (
                        or_(
                            Case.last_detail_checked_at.is_(None),
                            Case.last_detail_checked_at < cutoff,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("stale_count"),
        )
        .filter(Case.competencia == "civil")
        .group_by(Case.lawyer_id)
        .subquery()
    )

    results = (
        db.query(Lawyer, sub.c.case_count, sub.c.last_synced_at, sub.c.stale_count)
        .join(sub, Lawyer.id == sub.c.lawyer_id)
        .order_by(sub.c.last_synced_at.asc().nulls_first())
        .all()
    )

    return [
        SyncStatusItem(
            id=lawyer.id,
            rut=lawyer.rut,
            name=lawyer.name,
            email=lawyer.email,
            role=lawyer.role,
            case_count=case_count,
            last_synced_at=last_synced_at,
            stale_count=int(stale_count) if stale_count is not None else 0,
        )
        for lawyer, case_count, last_synced_at, stale_count in results
    ]


@router.get("/accounts", dependencies=[Depends(require_admin)])
def list_accounts(db: Session = Depends(get_db)):
    """List all lawyer accounts with has_password flag (admin only)."""
    lawyers = db.query(Lawyer).order_by(Lawyer.id).all()
    return [
        {
            "id": l.id,
            "rut": l.rut,
            "name": l.name,
            "email": l.email,
            "role": l.role,
            "nivel": l.nivel,
            "is_active": l.is_active is not False,
            "has_password": l.password_hash is not None,
        }
        for l in lawyers
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_lawyer(
    body: LawyerCreateBody,
    _admin_rut: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Add a person to the firm (admin only). No password, hito link or PJUD credential is
    created: the admin issues those afterwards with the existing actions."""
    existing = db.query(Lawyer).filter(Lawyer.rut == body.rut).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Ya existe una persona con ese RUT: {existing.name}")
    if body.email and db.query(Lawyer).filter(Lawyer.email == body.email).first() is not None:
        raise HTTPException(status_code=409, detail="Ya existe una persona con ese correo")

    lawyer = Lawyer(
        rut=body.rut, name=body.name, email=body.email, nivel=body.nivel,
        role="lawyer", is_firm_lawyer=True, is_active=True,
    )
    db.add(lawyer)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Ya existe una persona con ese RUT o correo")
    db.refresh(lawyer)
    return {
        "id": lawyer.id,
        "rut": lawyer.rut,
        "name": lawyer.name,
        "email": lawyer.email,
        "role": lawyer.role,
        "nivel": lawyer.nivel,
        "has_password": lawyer.password_hash is not None,
    }


@router.put("/{id}/account")
def update_account(
    id: int,
    body: AccountUpdateBody,
    admin_rut: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update a lawyer's email, role, and/or password (admin only)."""
    lawyer = db.query(Lawyer).filter(Lawyer.id == id).first()
    if not lawyer:
        raise HTTPException(status_code=404, detail="Lawyer not found")

    if body.role is not None:
        if body.role not in {"admin", "lawyer", "auditor"}:
            raise HTTPException(status_code=422, detail="Invalid role; must be 'admin', 'lawyer', or 'auditor'")
        lawyer.role = body.role

    if body.email is not None:
        lawyer.email = body.email

    if body.is_active is not None:
        if body.is_active is False:
            if normalize_rut(lawyer.rut) == normalize_rut(admin_rut):
                raise HTTPException(status_code=409, detail="No puedes bloquear tu propia cuenta")
            # The public hito link dies with the access; reactivating issues a new one if needed.
            lawyer.hito_form_token = None
        lawyer.is_active = body.is_active

    if body.new_password is not None:
        if len(body.new_password) < 8:
            raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
        lawyer.password_hash = hash_password(body.new_password)

    db.commit()
    db.refresh(lawyer)
    return {
        "id": lawyer.id,
        "rut": lawyer.rut,
        "name": lawyer.name,
        "email": lawyer.email,
        "role": lawyer.role,
        "nivel": lawyer.nivel,
        "is_active": lawyer.is_active is not False,
        "has_password": lawyer.password_hash is not None,
    }


@router.get("", response_model=list[LawyerRosterItem])
async def list_firm_lawyers(
    db: Session = Depends(get_db),
    current_lawyer: dict = Depends(get_current_lawyer),
):
    """Return the firm roster: co-side abogados across all of the account's cases."""
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)
    lawyer = db.get(Lawyer, lawyer_id)
    if not lawyer:
        raise HTTPException(status_code=404, detail="Lawyer not found")
    return firm_roster(db, lawyer.rut)
