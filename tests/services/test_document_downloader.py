"""Tests for DocumentDownloader — failure isolation, throttle, DOC_DOWNLOAD_ENABLED gate.

Strategy: mock scraper.download_document_generic + StorageService.upload.
No network calls, no Playwright, no disk I/O.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_pending_doc(doc_id: int, endpoint="documentos/docuS.php", token="FAKE_TOKEN"):
    """Return a minimal Document-like mock with status='pending'."""
    doc = MagicMock()
    doc.id = doc_id
    doc.status = "pending"
    doc.pjud_endpoint = endpoint
    doc.pjud_token = token
    doc.doc_type = "resolution"
    doc.case_id = 1
    doc.pjud_token_hash = f"abc123def{doc_id:03d}"
    return doc


def _make_scraper(side_effects):
    """Return a mock scraper whose download_document_generic yields side_effects in order."""
    scraper = MagicMock()
    scraper.download_document_generic = AsyncMock(side_effect=side_effects)
    return scraper


def _make_limiter():
    limiter = MagicMock()
    limiter.wait = AsyncMock()
    return limiter


def _make_storage_svc():
    svc = MagicMock()
    svc.upload = MagicMock()
    return svc


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestDocumentDownloaderFailureIsolation:
    @pytest.mark.asyncio
    async def test_second_doc_fails_others_still_stored(self):
        """Download failure on doc 2 → status=failed; docs 1 & 3 are stored."""
        import httpx

        doc1 = _make_pending_doc(1)
        doc2 = _make_pending_doc(2)
        doc3 = _make_pending_doc(3)

        scraper = _make_scraper([
            b"%PDF content1",
            httpx.RequestError("network error"),
            b"%PDF content3",
        ])
        db = MagicMock()
        storage_svc = _make_storage_svc()
        limiter = _make_limiter()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1, doc2, doc3],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=db,
            storage_service=storage_svc,
            limiter=limiter,
            enabled=True,
        )

        # Docs 1 and 3 should have been uploaded
        storage_svc.upload.assert_any_call(doc1, b"%PDF content1")
        storage_svc.upload.assert_any_call(doc3, b"%PDF content3")
        # Doc 2 must be marked failed
        assert doc2.status == "failed"
        # Function must not have raised (we got here)

    @pytest.mark.asyncio
    async def test_no_exception_propagated_when_download_fails(self):
        """Even if all docs fail, download_and_store must not raise."""
        import httpx

        doc1 = _make_pending_doc(1)
        scraper = _make_scraper([httpx.RequestError("fail")])
        db = MagicMock()

        from app.services.document_downloader import DocumentDownloader
        # Must not raise:
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=db,
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=True,
        )
        assert doc1.status == "failed"


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class TestDocumentDownloaderRateLimiter:
    @pytest.mark.asyncio
    async def test_limiter_wait_called_for_each_doc(self):
        """limiter.wait() must be called once per pending document."""
        doc1 = _make_pending_doc(1)
        doc2 = _make_pending_doc(2)

        scraper = _make_scraper([b"%PDF", b"%PDF"])
        db = MagicMock()
        storage_svc = _make_storage_svc()
        limiter = _make_limiter()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1, doc2],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=db,
            storage_service=storage_svc,
            limiter=limiter,
            enabled=True,
        )

        assert limiter.wait.call_count >= 1


# ---------------------------------------------------------------------------
# DOC_DOWNLOAD_ENABLED gate
# ---------------------------------------------------------------------------

class TestDocumentDownloaderEnabledGate:
    @pytest.mark.asyncio
    async def test_enabled_false_skips_all_downloads(self):
        """When enabled=False, scraper.download_document_generic must not be called."""
        doc1 = _make_pending_doc(1)
        doc2 = _make_pending_doc(2)
        scraper = _make_scraper([])  # empty side_effects; any call would raise StopIteration

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1, doc2],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=MagicMock(),
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=False,
        )

        scraper.download_document_generic.assert_not_called()

    @pytest.mark.asyncio
    async def test_unavailable_docs_are_skipped(self):
        """Docs with status='unavailable' must not be downloaded."""
        doc1 = _make_pending_doc(1)
        doc1.status = "unavailable"
        scraper = MagicMock()
        scraper.download_document_generic = AsyncMock()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=MagicMock(),
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=True,
        )

        scraper.download_document_generic.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_stored_docs_are_skipped(self):
        """Docs with status='stored' must not be re-downloaded."""
        doc1 = _make_pending_doc(1)
        doc1.status = "stored"
        scraper = MagicMock()
        scraper.download_document_generic = AsyncMock()

        from app.services.document_downloader import DocumentDownloader
        await DocumentDownloader().download_and_store(
            pending_docs=[doc1],
            scraper=scraper,
            pjud_session=MagicMock(),
            db=MagicMock(),
            storage_service=_make_storage_svc(),
            limiter=_make_limiter(),
            enabled=True,
        )

        scraper.download_document_generic.assert_not_called()
