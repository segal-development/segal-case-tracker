"""Courts endpoints - Reference data for Chilean courts."""

from typing import List
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.court import CourtResponse

router = APIRouter()


@router.get("", response_model=List[CourtResponse])
async def list_courts(
    region: str | None = Query(None, description="Filter by region code"),
    type: str | None = Query(None, description="Filter by court type (civil, familia, etc)"),
    db: Session = Depends(get_db),
):
    """
    List all available courts.
    
    Courts are seeded from PJUD data and include:
    - Juzgados Civiles
    - Juzgados de Letras
    - etc.
    """
    # TODO: Implement court listing with filters
    
    return [
        CourtResponse(
            id=1,
            code="1JCS",
            name="1er Juzgado Civil de Santiago",
            region="RM",
            type="civil",
        ),
        CourtResponse(
            id=2,
            code="2JCS",
            name="2do Juzgado Civil de Santiago",
            region="RM",
            type="civil",
        ),
    ]


@router.get("/{court_id}", response_model=CourtResponse)
async def get_court(
    court_id: int,
    db: Session = Depends(get_db),
):
    """Get a specific court by ID."""
    # TODO: Implement court retrieval
    
    return CourtResponse(
        id=court_id,
        code="1JCS",
        name="1er Juzgado Civil de Santiago",
        region="RM",
        type="civil",
    )
