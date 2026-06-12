"""Document download-and-store service (Slice 2).

Downloads pending documents synchronously within the 1-hour JWT window and
stores them via StorageService.  Failure on any single document is isolated:
the downloader logs a warning, marks that document as 'failed', and continues
with the remaining documents — the sync is never aborted.

Design (ADR-5):
- Runs inside detect_and_sync_movements immediately after persist_from_detail
  while the live browser page and freshly-parsed JWT tokens are still valid.
- Each download is isolated in try/except; a failed document does NOT poison
  the outer sync transaction.
- Rate-limited via an injectable limiter (AsyncSleep or TokenBucketLimiter).
- Gated by settings.DOC_DOWNLOAD_ENABLED (default False in dev/test).
"""

from __future__ import annotations

import logging
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.document import Document
    from app.services.storage_service import StorageService

logger = logging.getLogger(__name__)


class DocumentDownloader:
    """Fetch each pending document from PJUD and store it via StorageService.

    Responsibilities:
    - Gate on ``enabled`` — return immediately when False.
    - Skip docs that are not ``status="pending"`` (already stored, failed, unavailable).
    - Rate-limit between downloads via the injected ``limiter``.
    - Isolate per-document failures: mark ``status="failed"`` and continue.
    """

    async def download_and_store(
        self,
        pending_docs: List["Document"],
        scraper,
        pjud_session,
        db,
        storage_service: "StorageService",
        limiter,
        enabled: bool,
    ) -> None:
        """Download and store all eligible documents.

        Args:
            pending_docs:    All documents returned by persist_from_detail.
                             Only those with ``status="pending"`` are processed.
            scraper:         CivilScraper (or equivalent) — must expose
                             ``download_document_generic(session, endpoint, doc_type, token)``.
            pjud_session:    Active PJUDSession — forwarded to the scraper.
            db:              SQLAlchemy session for ``doc.status`` updates.
                             This method calls ``db.commit()`` after each
                             document (success or failure) so a crash mid-loop
                             does not leave uncommitted status updates.
            storage_service: StorageService — called with ``upload(doc, bytes)``.
            limiter:         Rate limiter; must expose an async ``wait()`` method.
                             Use a pass-through ``AsyncSleep(0)`` in tests or
                             when the scraper already throttles itself.
            enabled:         DOC_DOWNLOAD_ENABLED flag.  When ``False`` this
                             method returns immediately without touching PJUD.
        """
        if not enabled:
            logger.debug("DocumentDownloader: DOC_DOWNLOAD_ENABLED=False — skipping all")
            return

        for doc in pending_docs:
            # FIX 6: retry "failed" docs (transient errors); skip "stored" and "unavailable"
            if doc.status not in ("pending", "failed"):
                logger.debug(
                    "DocumentDownloader: skipping doc %s (status=%s)", doc.id, doc.status
                )
                continue

            if not doc.pjud_endpoint or not doc.pjud_token:
                logger.warning(
                    "DocumentDownloader: doc %s has no endpoint/token — marking failed",
                    doc.id,
                )
                doc.status = "failed"
                db.commit()
                continue

            # Rate-limit between downloads.
            await limiter.wait()

            try:
                pdf_bytes = await scraper.download_document_generic(
                    session=pjud_session,
                    endpoint=doc.pjud_endpoint,
                    doc_type=doc.doc_type or "resolution",
                    token=doc.pjud_token,
                )
                storage_service.upload(doc, pdf_bytes)
                db.commit()
                logger.info(
                    "DocumentDownloader: stored doc %s (%d bytes) → %s",
                    doc.id,
                    len(pdf_bytes),
                    doc.gcs_path,
                )
            except Exception as exc:  # noqa: BLE001 — failure isolation
                logger.warning(
                    "DocumentDownloader: failed to download/store doc %s: %s",
                    doc.id,
                    exc,
                )
                doc.status = "failed"
                db.commit()


class AsyncSleepLimiter:
    """Minimal rate limiter that always yields control via asyncio.sleep.

    FIX 7: ``asyncio.sleep(0)`` is called even when *delay* is zero so that
    the event loop always gets a chance to run other tasks (no tight loops).

    FIX 8: renamed from ``_AsyncSleepLimiter`` (public name — imported across
    module boundaries from sync_service).
    """

    def __init__(self, delay: float = 0.0) -> None:
        self._delay = delay

    async def wait(self) -> None:
        import asyncio
        await asyncio.sleep(self._delay)  # FIX 7: always yield, even when delay=0
