"""Tests for Slice 2 document API — serve from storage + redirect endpoint.

Endpoints under test:
    GET /api/v1/documents/{document_id}          (new — 307 redirect to signed URL)
    GET /api/v1/documents/{document_id}/download (modified — storage-first, PJUD fallback)

Strategy: mock StorageService and scraper — no disk I/O, no network, no Playwright.
"""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="55555555-5", name="Storage Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T-STG", name="Juzgado Storage", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def case_obj(db, lawyer, court):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-0042-2026",
        status="active",
        competencia="civil",
        plaintiff="DEMANDANTE",
        defendant="DEMANDADO",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def stored_document(db, case_obj):
    """A document that has been downloaded and stored locally."""
    obj = Document(
        case_id=case_obj.id,
        doc_type="resolution",
        pjud_endpoint="documentos/docuS.php",
        pjud_token="eyJhbGciOiJub25lIn0.e30.FAKE",
        pjud_token_hash="aabbccdd112233",
        gcs_path="cases/1/resolution_aabbccdd1122.pdf",
        status="stored",
        filename="resolucion.pdf",
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def pending_document(db, case_obj):
    """A document with status=pending (not yet downloaded)."""
    obj = Document(
        case_id=case_obj.id,
        doc_type="cert_envio",
        pjud_endpoint="documentos/docCertificadoDemanda.php",
        pjud_token="eyJhbGciOiJub25lIn0.e30.FAKE2",
        pjud_token_hash="eeff99887766",
        status="pending",
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def unavailable_document(db, case_obj):
    obj = Document(
        case_id=case_obj.id,
        doc_type="ebook",
        pjud_endpoint=None,
        pjud_token=None,
        status="unavailable",
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def authed_client(client, lawyer):
    """TestClient with get_current_lawyer stubbed to the test lawyer."""
    async def _mock_lawyer():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock_lawyer
    yield client


# ---------------------------------------------------------------------------
# GET /api/v1/documents/{document_id}  — redirect to signed URL
# ---------------------------------------------------------------------------

class TestGetDocumentRedirect:
    def test_stored_document_returns_307_redirect(self, authed_client, stored_document):
        """A stored document returns a 307 redirect to the signed URL."""
        fake_url = "https://storage.googleapis.com/my-bucket/file.pdf?X-Goog-Signature=abc"

        with patch(
            "app.api.v1.documents.StorageService.signed_url",
            return_value=fake_url,
        ):
            resp = authed_client.get(
                f"/api/v1/documents/{stored_document.id}",
                follow_redirects=False,
            )

        assert resp.status_code in (302, 307)
        assert resp.headers.get("location") == fake_url

    def test_unknown_document_returns_404(self, authed_client):
        """Non-existent document ID returns 404."""
        resp = authed_client.get("/api/v1/documents/99999", follow_redirects=False)
        assert resp.status_code == 404

    def test_pending_document_returns_404(self, authed_client, pending_document):
        """A document with status=pending (not yet stored) returns 404."""
        resp = authed_client.get(
            f"/api/v1/documents/{pending_document.id}", follow_redirects=False
        )
        assert resp.status_code == 404

    def test_unavailable_document_returns_404(self, authed_client, unavailable_document):
        """A document marked unavailable returns 404."""
        resp = authed_client.get(
            f"/api/v1/documents/{unavailable_document.id}", follow_redirects=False
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/documents/{document_id}/download  — storage-first + fallback
# ---------------------------------------------------------------------------

_FAKE_PDF = b"%PDF-1.4 fake pdf content"
_FAKE_SESSION = MagicMock()


def _make_session_store_mock(session=_FAKE_SESSION):
    store = MagicMock()
    store.get_session_by_lawyer = AsyncMock(return_value=session)
    return store


class TestDownloadDocumentFromStorage:
    def test_stored_document_served_without_pjud_call(
        self, authed_client, stored_document
    ):
        """A stored document is served from local storage; scraper is never called."""
        with (
            patch(
                "app.api.v1.documents.StorageService.retrieve",
                return_value=_FAKE_PDF,
            ),
            patch(
                "app.api.v1.documents.get_session_store"
            ) as mock_store,
            patch(
                "app.api.v1.documents.BrowserFactory"
            ) as mock_factory,
            patch(
                "app.api.v1.documents.CivilScraper"
            ) as mock_scraper_cls,
        ):
            resp = authed_client.get(
                f"/api/v1/documents/{stored_document.id}/download"
            )

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == _FAKE_PDF
        # PJUD should never have been touched
        mock_store.assert_not_called()
        mock_factory.assert_not_called()
        mock_scraper_cls.assert_not_called()

    def test_pending_document_falls_back_to_pjud(
        self, authed_client, pending_document
    ):
        """A non-stored document falls back to live PJUD download."""
        store_mock = _make_session_store_mock()

        with (
            patch(
                "app.api.v1.documents.get_session_store",
                return_value=store_mock,
            ),
            patch(
                "app.api.v1.documents.BrowserFactory"
            ) as mock_factory_cls,
            patch(
                "app.api.v1.documents.CivilScraper"
            ) as mock_scraper_cls,
        ):
            mock_factory = MagicMock()
            mock_factory.__aenter__ = AsyncMock(return_value=mock_factory)
            mock_factory.__aexit__ = AsyncMock(return_value=False)
            mock_factory.new_page = AsyncMock(return_value=MagicMock())
            mock_factory._browser = MagicMock()
            mock_factory._context = MagicMock()
            mock_factory_cls.return_value = mock_factory

            mock_scraper = MagicMock()
            mock_scraper.download_document_generic = AsyncMock(return_value=_FAKE_PDF)
            mock_scraper_cls.return_value = mock_scraper

            resp = authed_client.get(
                f"/api/v1/documents/{pending_document.id}/download"
            )

        assert resp.status_code == 200
        assert resp.content == _FAKE_PDF
        # Scraper WAS called for the live fallback
        mock_scraper.download_document_generic.assert_called_once()
