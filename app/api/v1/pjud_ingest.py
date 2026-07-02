"""PJUD ingest endpoints for the browser extension (Slice 1: cases only).

Authenticated via ``X-Ingest-Key`` (require_ingest_key), NOT lawyer JWT —
the caller is the extension's service worker relaying raw HTML fetched
from the operator's own authenticated PJUD session, not a logged-in lawyer.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_ingest_key
from app.services.ingest_service import IngestParseError, IngestService

router = APIRouter()


class IngestCasesRequest(BaseModel):
    """Payload relayed by the extension: raw HTML pages + lawyer identifier."""

    rut: str = Field(..., description="Lawyer RUT the cases belong to")
    competencia: str = Field("civil", description="Only 'civil' is supported in Slice 1")
    pages: List[str] = Field(..., min_length=1, description="Raw Mis Causas HTML pages")


class IngestCasesResponse(BaseModel):
    new: int
    existing: int
    errors: List[str]


@router.post("/cases", response_model=IngestCasesResponse)
def ingest_cases(
    body: IngestCasesRequest,
    db: Session = Depends(get_db),
    _ingest_key=Depends(require_ingest_key),
):
    """Parse raw Mis Causas civil HTML and bulk-upsert cases for a lawyer."""
    service = IngestService(db)
    try:
        result = service.ingest_cases(
            lawyer_rut=body.rut, competencia=body.competencia, pages=body.pages
        )
    except IngestParseError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return IngestCasesResponse(**result)
