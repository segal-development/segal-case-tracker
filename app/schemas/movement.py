"""Movement schemas."""

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel


class MovementResponse(BaseModel):
    """Case movement response."""
    
    id: int
    case_id: int
    stage: str | None = None
    procedure: str | None = None
    description: str
    document_url: str | None = None
    folio: str | None = None
    movement_date: str
    created_at: str


class MovementListResponse(BaseModel):
    """Paginated movement list response."""
    
    items: List[MovementResponse]
    total: int
    page: int
    per_page: int
    pages: int
