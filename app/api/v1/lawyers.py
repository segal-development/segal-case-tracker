"""Lawyers endpoint — firm roster for the authenticated account."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_current_lawyer, _resolve_lawyer_id
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import firm_roster

router = APIRouter()


class LawyerRosterItem(BaseModel):
    rut: str
    nombre: str
    case_count: int

    model_config = ConfigDict(from_attributes=True)


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
