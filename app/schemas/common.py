"""Common schemas - Pagination, errors, jobs, etc."""

from typing import Any, Generic, List, Optional, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response."""
    
    items: List[T]
    total: int
    page: int
    per_page: int
    pages: int


class ErrorResponse(BaseModel):
    """Error response."""
    
    detail: str
    code: str | None = None


class JobResponse(BaseModel):
    """Job creation response."""
    
    job_id: str
    status: str  # pending, processing, completed, failed
    message: str | None = None


class JobStatusResponse(BaseModel):
    """Job status polling response."""
    
    job_id: str
    status: str  # pending, processing, completed, failed
    progress: int = 0  # 0-100
    results: Any | None = None
    error: str | None = None
