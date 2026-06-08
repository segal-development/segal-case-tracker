"""Cases CRUD endpoints."""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_current_lawyer
from app.schemas.case import CaseCreate, CaseResponse, CaseListResponse
from app.schemas.movement import MovementResponse

router = APIRouter()


@router.get("", response_model=CaseListResponse)
async def list_cases(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    court_id: int | None = None,
    status: str | None = None,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """List all cases for the authenticated lawyer."""
    # TODO: Implement case listing with filters
    
    return CaseListResponse(
        items=[],
        total=0,
        page=page,
        per_page=per_page,
        pages=0,
    )


@router.post("", response_model=CaseResponse, status_code=status.HTTP_201_CREATED)
async def create_case(
    case_data: CaseCreate,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Create or link a case to the lawyer's account."""
    # TODO: Implement case creation/linking
    
    return CaseResponse(
        id=1,
        rol="C-1234-2024",
        court_id=case_data.court_id,
        court_name="1er Juzgado Civil de Santiago",
        plaintiff="Demandante Ejemplo",
        defendant="Demandado Ejemplo",
        matter="Cobro de pesos",
        status="active",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Get a specific case by ID."""
    # TODO: Implement case retrieval
    
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Case not found",
    )


@router.get("/{case_id}/movements", response_model=List[MovementResponse])
async def get_case_movements(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Get all movements for a case."""
    # TODO: Implement movement listing
    
    return []


@router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_case(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Unlink a case from the lawyer's account."""
    # TODO: Implement case unlinking (soft delete)
    pass
