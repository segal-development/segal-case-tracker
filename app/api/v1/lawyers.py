"""Lawyers endpoint — firm roster and admin account management."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_current_lawyer, require_admin, _resolve_lawyer_id
from app.core.security import hash_password
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import firm_roster

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
            "has_password": l.password_hash is not None,
        }
        for l in lawyers
    ]


@router.put("/{id}/account")
def update_account(
    id: int,
    body: AccountUpdateBody,
    _admin_rut: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update a lawyer's email, role, and/or password (admin only)."""
    lawyer = db.query(Lawyer).filter(Lawyer.id == id).first()
    if not lawyer:
        raise HTTPException(status_code=404, detail="Lawyer not found")

    if body.role is not None:
        if body.role not in {"admin", "lawyer"}:
            raise HTTPException(status_code=422, detail="Invalid role; must be 'admin' or 'lawyer'")
        lawyer.role = body.role

    if body.email is not None:
        lawyer.email = body.email

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
