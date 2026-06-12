"""Document download endpoint.

GET /api/v1/documents/{document_id}/download

Streams a live PJUD PDF using the authenticated browser session stored in
Redis.  No bytes are persisted — this is a live-download-and-stream path
intended for demo use.
"""

import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_lawyer, get_db
from app.models.document import Document
from app.models.case import Case
from app.scrapper.pjud.civil import CivilScraper
from app.scrapper.pjud.browser import BrowserFactory
from app.scrapper.pjud.exceptions import DocumentTokenExpiredError, ScrapingError
from app.services.session_store import get_session_store

router = APIRouter()
_logger = logging.getLogger(__name__)


def _pdf_stream(data: bytes) -> AsyncIterator[bytes]:
    """Wrap bytes in a one-shot async iterator for StreamingResponse."""
    async def _gen():
        yield data
    return _gen()


@router.get("/{document_id}/download")
async def download_document(
    document_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Stream a PJUD document PDF to the caller.

    Requires an active PJUD browser session in the Redis session store
    (obtained via POST /api/v1/pjud/login).

    Error codes:
        404 — document not found (or not owned by the authenticated lawyer).
        409 — no active PJUD session; the lawyer must log in again.
        410 — document token expired; re-sync the case to refresh tokens.
        500 — unexpected download failure.
    """
    lawyer_id = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if not lawyer_id:
        raise HTTPException(status_code=401, detail="Invalid token: no lawyer_id")

    # ── 1. Load document and verify ownership via the parent case ──────────
    doc = (
        db.query(Document)
        .join(Case, Case.id == Document.case_id)
        .filter(
            Document.id == document_id,
            Case.lawyer_id == lawyer_id,
        )
        .first()
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    if not doc.pjud_endpoint or not doc.pjud_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document has no PJUD token — re-sync the case first",
        )

    # ── 2. Resolve the active PJUD session for this lawyer ────────────────
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

    # ── 3. Download via the authenticated browser ──────────────────────────
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

    # ── 4. Stream the PDF back ─────────────────────────────────────────────
    safe_name = (doc.filename or f"document_{document_id}").replace(" ", "_")
    if not safe_name.endswith(".pdf"):
        safe_name += ".pdf"

    return StreamingResponse(
        _pdf_stream(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename={safe_name}"},
    )
