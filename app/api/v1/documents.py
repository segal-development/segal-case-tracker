"""Document endpoints.

GET /api/v1/documents/{document_id}
    Redirect (307) to a signed URL when the document is stored.
    Returns 404 if the document does not exist or is not yet stored.

GET /api/v1/documents/{document_id}/download
    Serve document bytes.
    - Primary path: if doc.status == "stored", retrieve bytes from StorageService
      directly — no PJUD call, no session required.
    - Fallback path: if not stored and an active PJUD session is available,
      download live from PJUD (pre-Slice-2 behaviour, kept for graceful degradation).
    - Returns 409 if not stored and no session is available.
"""

import logging
from datetime import date, datetime
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import DateTime, func, text as sql_text
from sqlalchemy.orm import Session

from app.api.deps import (
    apply_case_scope,
    get_current_lawyer,
    get_db,
    resolve_case_scope,
    _resolve_lawyer_id,
)
from app.config import settings
from app.models.document import Document
from app.models.case import Case
from app.models.movement import Movement
from app.scrapper.pjud.civil import CivilScraper
from app.scrapper.pjud.browser import BrowserFactory
from app.scrapper.pjud.exceptions import DocumentTokenExpiredError, ScrapingError
from app.services.session_store import get_session_store
from app.services.storage_service import StorageService, get_storage_backend

router = APIRouter()
_logger = logging.getLogger(__name__)


def _pdf_stream(data: bytes) -> AsyncIterator[bytes]:
    """Wrap bytes in a one-shot async iterator for StreamingResponse."""
    async def _gen():
        yield data
    return _gen()


def _get_storage_service() -> StorageService:
    """Dependency: return a StorageService backed by the configured backend."""
    return StorageService(get_storage_backend(settings))


# ---------------------------------------------------------------------------
# GET /search — full-text search over extracted document text
# ---------------------------------------------------------------------------
#
# NOTE: this route MUST be registered BEFORE ``/{document_id}`` so that the
# literal path segment "search" is not captured as a document id.


class DocumentSearchHit(BaseModel):
    """One full-text search result."""

    document_id: int
    case_id: int
    case_rol: str
    doc_type: Optional[str] = None
    filename: Optional[str] = None
    fecha: Optional[date] = None
    snippet: str
    download_url: str


def _sqlite_snippet(texto: str, q: str, width: int = 200) -> str:
    """Build a ~``width``-char window around the first case-insensitive match.

    Used on SQLite (tests), where there is no ``ts_headline``. On PostgreSQL
    the snippet comes from ``ts_headline`` instead.
    """
    if not texto:
        return ""
    low = texto.lower()
    idx = low.find(q.lower())
    if idx == -1:
        return texto[:width].strip()
    half = max(0, (width - len(q)) // 2)
    start = max(0, idx - half)
    end = min(len(texto), idx + len(q) + half)
    snip = texto[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(texto) else ""
    return f"{prefix}{snip}{suffix}"


def _as_date(value) -> Optional[date]:
    """Coerce a datetime/date/None search value into a ``date`` (or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


@router.get("/search", response_model=List[DocumentSearchHit])
async def search_documents(
    q: str = Query(..., min_length=2, description="Search query (min 2 chars)"),
    case_id: Optional[int] = Query(None, description="Restrict to a single case"),
    limit: int = Query(30, ge=1, le=100, description="Max results (cap 100)"),
    orden: str = Query(
        "relevancia",
        description='Ordering: "relevancia" (default) or "reciente" (newest first)',
    ),
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
) -> List[DocumentSearchHit]:
    """Full-text search over extracted document text.

    Only documents whose ``texto`` has been extracted (``texto IS NOT NULL``)
    are searchable. Results are scoped exactly like the other document
    endpoints (auditor/admin see everything; a regular lawyer sees the cases
    they are an abogado-of-record litigante on).

    Each hit carries a ``fecha`` — the document's own ``document_date`` when
    present, otherwise the linked movement's ``movement_date`` (LEFT JOIN, so
    docs without a movement are not dropped), or ``null`` when neither exists.

    ``orden`` controls ordering:
      - ``"relevancia"`` (default): PG ``ts_rank`` DESC / SQLite by id.
      - ``"reciente"``: by ``fecha`` DESC, NULLs last.
    Any other value is clamped to ``"relevancia"``.

    Dialect-aware:
      - PostgreSQL: Spanish ``to_tsvector @@ websearch_to_tsquery`` match,
        ordered by ``ts_rank``, snippet from ``ts_headline``.
      - SQLite (tests): case-insensitive ``LIKE`` scan, ordered by id, snippet
        built in Python.
    """
    scope = resolve_case_scope(db, current_lawyer)
    dialect = db.get_bind().dialect.name

    # Clamp unknown ordering values to the default ("relevancia").
    if orden != "reciente":
        orden = "relevancia"

    # Document date = own document_date, else linked movement's movement_date.
    fecha_expr = func.coalesce(
        Document.document_date, Movement.movement_date, type_=DateTime()
    )

    if dialect == "postgresql":
        cfg = sql_text("'spanish'")  # inline regconfig literal (NOT interpolated q)
        tsvector = func.to_tsvector(cfg, func.coalesce(Document.texto, ""))
        tsquery = func.websearch_to_tsquery(cfg, q)  # q -> bound param
        headline = func.ts_headline(
            cfg,
            Document.texto,
            tsquery,
            "MaxWords=30, MinWords=12, ShortWord=2",
        )
        query = (
            db.query(Document, Case.rol, headline.label("snippet"), fecha_expr.label("fecha"))
            .join(Case, Case.id == Document.case_id)
            .outerjoin(Movement, Document.movement_id == Movement.id)
            .filter(Document.texto.isnot(None))
            .filter(tsvector.op("@@")(tsquery))
        )
        query = apply_case_scope(query, scope)
        if case_id is not None:
            query = query.filter(Document.case_id == case_id)
        if orden == "reciente":
            query = query.order_by(fecha_expr.desc().nullslast())
        else:
            query = query.order_by(func.ts_rank(tsvector, tsquery).desc())
        rows = query.limit(limit).all()

        return [
            DocumentSearchHit(
                document_id=doc.id,
                case_id=doc.case_id,
                case_rol=rol,
                doc_type=doc.doc_type,
                filename=doc.filename,
                fecha=_as_date(fecha),
                snippet=snippet or "",
                download_url=f"/api/v1/documents/{doc.id}/download",
            )
            for doc, rol, snippet, fecha in rows
        ]

    # SQLite / fallback: case-insensitive LIKE (value is bound, not interpolated).
    query = (
        db.query(Document, Case.rol, fecha_expr.label("fecha"))
        .join(Case, Case.id == Document.case_id)
        .outerjoin(Movement, Document.movement_id == Movement.id)
        .filter(Document.texto.isnot(None))
        .filter(func.lower(Document.texto).like(f"%{q.lower()}%"))
    )
    query = apply_case_scope(query, scope)
    if case_id is not None:
        query = query.filter(Document.case_id == case_id)
    if orden == "reciente":
        # Emulate NULLS LAST: null dates sort after non-null, then newest first.
        query = query.order_by((fecha_expr.is_(None)).asc(), fecha_expr.desc())
    else:
        query = query.order_by(Document.id)
    rows = query.limit(limit).all()

    return [
        DocumentSearchHit(
            document_id=doc.id,
            case_id=doc.case_id,
            case_rol=rol,
            doc_type=doc.doc_type,
            filename=doc.filename,
            fecha=_as_date(fecha),
            snippet=_sqlite_snippet(doc.texto or "", q),
            download_url=f"/api/v1/documents/{doc.id}/download",
        )
        for doc, rol, fecha in rows
    ]


# ---------------------------------------------------------------------------
# GET /{document_id} — redirect to signed URL
# ---------------------------------------------------------------------------

@router.get("/{document_id}")
async def get_document_redirect(
    document_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
    storage_svc: StorageService = Depends(_get_storage_service),
):
    """Redirect to a signed URL for a stored document.

    Returns:
        307 Temporary Redirect — ``Location`` header points to the signed URL.

    Raises:
        404 — document not found, not visible to caller, or not yet stored.
    """
    scope = resolve_case_scope(db, current_lawyer)

    doc = apply_case_scope(
        db.query(Document).join(Case, Case.id == Document.case_id).filter(
            Document.id == document_id
        ),
        scope,
    ).first()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    if doc.status != "stored" or not doc.gcs_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not yet stored — re-sync to download",
        )

    url = storage_svc.signed_url(doc, settings.GCS_SIGNED_URL_TTL)
    if not url:
        raise HTTPException(status_code=500, detail="Could not generate signed URL")

    return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


# ---------------------------------------------------------------------------
# GET /{document_id}/download — storage-first, PJUD fallback
# ---------------------------------------------------------------------------

@router.get("/{document_id}/download")
async def download_document(
    document_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
    storage_svc: StorageService = Depends(_get_storage_service),
):
    """Stream a document PDF to the caller.

    Primary path (no session required):
        When ``doc.status == "stored"`` and ``doc.gcs_path`` is set, the bytes
        are retrieved from StorageService and streamed directly.  No PJUD call
        is made.

    Fallback path (session required):
        When the document is not yet stored, the endpoint falls back to a live
        PJUD download using the authenticated browser session stored in Redis.
        This preserves the pre-Slice-2 behavior for documents that have not
        been downloaded during sync yet.

    Error codes:
        404 — document not found or not visible to the authenticated lawyer.
        409 — document not stored AND no active PJUD session available.
        410 — document token expired; re-sync the case to refresh tokens.
        500 — unexpected download failure.
    """
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)
    if not lawyer_id:
        raise HTTPException(status_code=401, detail="Invalid token: no lawyer_id")

    # ── 1. Load document and verify visibility ──────────────────────────────
    # Authorized the same way the case/document LIST endpoints are — via the
    # litigante-based case scope (auditor/admin see every case) — not via
    # Case.lawyer_id, which under Approach C is the firm's single canonical
    # owner and never matches a regular lawyer's own id.
    scope = resolve_case_scope(db, current_lawyer)
    doc = apply_case_scope(
        db.query(Document).join(Case, Case.id == Document.case_id).filter(
            Document.id == document_id
        ),
        scope,
    ).first()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    # ── 2. Primary path: serve from storage ──────────────────────────────
    if doc.status == "stored" and doc.gcs_path:
        try:
            pdf_bytes = storage_svc.retrieve(doc.gcs_path)
            safe_name = (doc.filename or f"document_{document_id}").replace(" ", "_")
            if not safe_name.endswith(".pdf"):
                safe_name += ".pdf"
            return StreamingResponse(
                _pdf_stream(pdf_bytes),
                media_type="application/pdf",
                headers={"Content-Disposition": f"inline; filename={safe_name}"},
            )
        except Exception as exc:
            # FIX 5: do NOT fall through to PJUD for a doc marked "stored".
            # A re-sync cannot restore a file that is missing from storage.
            # Return 503 so the caller knows the storage layer is unavailable.
            _logger.error(
                "doc %s is marked stored but storage retrieval failed (%s): %s",
                document_id,
                doc.gcs_path,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Document storage unavailable — contact support",
            )

    # ── 3. Fallback: live PJUD download ───────────────────────────────────
    if not doc.pjud_endpoint or not doc.pjud_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document has no PJUD token — re-sync the case first",
        )

    try:
        lawyer_id_int = int(lawyer_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid lawyer_id in token")

    pjud_session = await get_session_store().get_session_by_lawyer(lawyer_id_int)
    if not pjud_session:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No active PJUD session — login again",
        )

    try:
        async with BrowserFactory() as factory:
            page = await factory.new_page(pjud_session)

            scraper = CivilScraper()
            scraper._page = page
            scraper._browser = factory._browser
            scraper._context = factory._context

            pdf_bytes = await scraper.download_document_generic(
                session=pjud_session,
                endpoint=doc.pjud_endpoint,
                doc_type=doc.doc_type or "resolution",
                token=doc.pjud_token,
            )

    except DocumentTokenExpiredError as exc:
        _logger.warning("Document %s token expired: %s", document_id, exc)
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Document token expired — re-sync the case",
        )
    except ScrapingError as exc:
        _logger.error("Scraping error for document %s: %s", document_id, exc)
        raise HTTPException(status_code=500, detail=f"Download failed: {exc}")
    except Exception as exc:
        _logger.error("Unexpected error for document %s: %s", document_id, exc)
        raise HTTPException(status_code=500, detail="Unexpected download error")

    safe_name = (doc.filename or f"document_{document_id}").replace(" ", "_")
    if not safe_name.endswith(".pdf"):
        safe_name += ".pdf"

    return StreamingResponse(
        _pdf_stream(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename={safe_name}"},
    )
