"""PJUD ingest endpoints for the browser extension.

Slice 1: ``POST /cases`` — bulk case ingest from Mis Causas listing HTML.
Slice 2: ``GET /pending-detail`` + ``POST /movements`` — stale-case rotation
and movements ingest from detail-modal HTML, driving deadline/semáforo
classification for cases that previously had no movement history.

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


class PendingDetailCase(BaseModel):
    id: int
    rol: str


class PendingDetailResponse(BaseModel):
    cases: List[PendingDetailCase]


class IngestMovementsCaseItem(BaseModel):
    """One detail-modal HTML capture keyed by ROL (no case_token needed —
    the extension re-resolves ``detalleMisCausaCivil`` in-page from a live
    session; the backend only needs the raw HTML + which case it belongs to).
    """

    rol: str
    html: str


class IngestMovementsRequest(BaseModel):
    rut: str = Field(..., description="Lawyer RUT the cases belong to")
    competencia: str = Field("civil", description="Only 'civil' is supported in Slice 2")
    # ``cases`` may be empty when the extension only has failed ROLs to report
    # (no case resolved a token this run); at least one of cases/failed_rols is
    # expected, but an all-empty payload is a harmless no-op.
    cases: List[IngestMovementsCaseItem] = Field(default_factory=list)
    failed_rols: List[str] = Field(
        default_factory=list,
        description="ROLs the extension could not resolve in the live Mis Causas "
        "list; the backend stamps them so they rotate out of future batches.",
    )


class IngestMovementsResponse(BaseModel):
    cases_processed: int
    movements_new: int
    classified: int
    failed_stamped: int
    errors: List[str]


class IngestDocumentItem(BaseModel):
    """One downloaded PDF relayed by the extension.

    ``token_hash`` is the STABLE identity — identical to
    ``Document.pjud_token_hash`` = ``sha256(doc_type|case_rol|scope_key)`` — so
    the backend matches the pending Document row without the rotating JWT.
    """

    rol: str = Field(..., description="Case ROL the document belongs to")
    token_hash: str = Field(..., description="Stable Document.pjud_token_hash to match")
    filename: str = Field(..., description="Original file name")
    content_type: str = Field("application/pdf", description="MIME type")
    document_date: Optional[str] = Field(
        None, description="Movement/document date (DD/MM/YYYY), optional"
    )
    base64: str = Field(..., description="Base64-encoded PDF bytes")


class IngestDocumentsRequest(BaseModel):
    rut: str = Field(..., description="Lawyer RUT the documents belong to")
    competencia: str = Field("civil", description="Only 'civil' is supported")
    documents: List[IngestDocumentItem] = Field(default_factory=list)


class IngestDocumentsResponse(BaseModel):
    documents_stored: int
    skipped: int
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


@router.get("/pending-detail", response_model=PendingDetailResponse)
def pending_detail(
    rut: str,
    competencia: str = "civil",
    limit: int = 30,
    db: Session = Depends(get_db),
    _ingest_key=Depends(require_ingest_key),
):
    """Return the stalest cases (by ``last_detail_checked_at``, NULLS FIRST)
    needing a movements refresh, so the extension knows which ROLs to open
    detail modals for in-page. Prioritizes never-checked cases.
    """
    service = IngestService(db)
    cases = service.get_pending_detail(lawyer_rut=rut, competencia=competencia, limit=limit)
    return PendingDetailResponse(cases=[PendingDetailCase(**c) for c in cases])


@router.post("/movements", response_model=IngestMovementsResponse)
def ingest_movements(
    body: IngestMovementsRequest,
    db: Session = Depends(get_db),
    _ingest_key=Depends(require_ingest_key),
):
    """Parse raw PJUD detail-modal HTML and persist movements + recompute the
    deadline/semáforo for each case, so previously-untracked cases become
    classified.
    """
    service = IngestService(db)
    result = service.ingest_movements(
        lawyer_rut=body.rut,
        competencia=body.competencia,
        cases=[c.model_dump() for c in body.cases],
        failed_rols=body.failed_rols,
    )
    return IngestMovementsResponse(**result)


@router.post("/documents", response_model=IngestDocumentsResponse)
def ingest_documents(
    body: IngestDocumentsRequest,
    db: Session = Depends(get_db),
    _ingest_key=Depends(require_ingest_key),
):
    """Store PDF binaries the extension downloaded in-browser against their
    pending Document rows (matched by the stable ``token_hash``). Per-item
    failures (unknown case/document, invalid base64) are collected in
    ``errors``/``skipped`` — the batch never raises.
    """
    service = IngestService(db)
    result = service.ingest_documents(
        lawyer_rut=body.rut,
        competencia=body.competencia,
        documents=[d.model_dump() for d in body.documents],
    )
    return IngestDocumentsResponse(**result)
