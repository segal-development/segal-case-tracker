"""Case schemas."""

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class CaseCreate(BaseModel):
    """Create/link a case."""
    
    rol: str = Field(..., description="Case ROL (e.g., C-1234-2024)")
    court_id: int = Field(..., description="Court ID")


class CaseResponse(BaseModel):
    """Case response."""
    
    id: int
    rol: str
    court_id: int
    court_name: str
    plaintiff: str | None = None
    defendant: str | None = None
    matter: str | None = None
    status: str
    created_at: str
    updated_at: str


class CaseListResponse(BaseModel):
    """Paginated case list response."""
    
    items: List[CaseResponse]
    total: int
    page: int
    per_page: int
    pages: int
