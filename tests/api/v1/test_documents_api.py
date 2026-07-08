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
    app.dependency_overrides.pop(get_current_lawyer, None)  # FIX 4: avoid leaking into other tests


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


class TestDownloadDocumentStorageFailure:
    def test_stored_doc_with_storage_error_returns_503_not_pjud(
        self, authed_client, stored_document
    ):
        """FIX 5: a stored doc whose storage retrieve raises must return 503; scraper must NOT run."""
        with (
            patch(
                "app.api.v1.documents.StorageService.retrieve",
                side_effect=OSError("disk read failed"),
            ),
            patch("app.api.v1.documents.get_session_store") as mock_store,
            patch("app.api.v1.documents.BrowserFactory") as mock_factory,
            patch("app.api.v1.documents.CivilScraper") as mock_scraper_cls,
        ):
            resp = authed_client.get(
                f"/api/v1/documents/{stored_document.id}/download"
            )

        assert resp.status_code == 503
        # PJUD fallback must NOT have been invoked
        mock_store.assert_not_called()
        mock_factory.assert_not_called()
        mock_scraper_cls.assert_not_called()


# ---------------------------------------------------------------------------
# FIX #4 — document authorization must follow the litigante-based case scope
# (resolve_case_scope/apply_case_scope), not Case.lawyer_id == acting lawyer.
#
# Under Approach C, Case.lawyer_id is the FIRM's single canonical owner, so a
# regular lawyer's own id never matches it — they could LIST a case's
# documents (litigante-scoped) but got a 404 downloading them. These tests
# seed a case owned by a different "firm" lawyer id and authorize via a
# CaseLitigante abogado-of-record row instead.
# ---------------------------------------------------------------------------


@pytest.fixture
def firm_owner(db):
    """The case's Case.lawyer_id owner — distinct from any acting lawyer id."""
    obj = Lawyer(rut="16021492-9", name="Firm Owner")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def litigante_abogado(db):
    """A regular lawyer who is an abogado-of-record litigante on the case,
    but does NOT own it via Case.lawyer_id."""
    obj = Lawyer(rut="66666666-6", name="Litigante Abogado")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def outsider_lawyer(db):
    """A lawyer with no relationship at all to the case."""
    obj = Lawyer(rut="77777777-7", name="Outsider Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def auditor_lawyer(db):
    obj = Lawyer(rut="88888888-8", name="Auditor", role="auditor")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def admin_lawyer(db):
    obj = Lawyer(rut="99999999-9", name="Admin", role="admin")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def firm_owned_case(db, firm_owner, court):
    """A case owned (Case.lawyer_id) by the firm account, mirroring Approach C."""
    obj = Case(
        lawyer_id=firm_owner.id,
        court_id=court.id,
        rol="C-4242-2026",
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
def litigante_row(db, firm_owned_case, litigante_abogado):
    from app.models.case_litigante import CaseLitigante

    row = CaseLitigante(
        case_id=firm_owned_case.id,
        participante="AB.DTE",
        rut=litigante_abogado.rut,
        persona_type="NATURAL",
        nombre=litigante_abogado.name,
        natural_key=f"{firm_owned_case.id}-abogado",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def firm_owned_stored_document(db, firm_owned_case):
    obj = Document(
        case_id=firm_owned_case.id,
        doc_type="resolution",
        pjud_endpoint="documentos/docuS.php",
        pjud_token="eyJhbGciOiJub25lIn0.e30.FIRM",
        pjud_token_hash="ffeedd0011",
        gcs_path="cases/firm/resolution.pdf",
        status="stored",
        filename="resolucion_firm.pdf",
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _client_as(client, sub: str):
    """TestClient with get_current_lawyer stubbed to the given JWT sub."""

    async def _mock():
        return {"sub": sub}

    app.dependency_overrides[get_current_lawyer] = _mock
    return client


class TestDocumentAuthorizationLitiganteScope:
    def test_redirect_allows_litigante_abogado_on_firm_owned_case(
        self, client, litigante_abogado, litigante_row, firm_owned_stored_document
    ):
        fake_url = "https://storage.googleapis.com/my-bucket/file.pdf?X-Goog-Signature=abc"
        _client_as(client, str(litigante_abogado.id))
        try:
            with patch(
                "app.api.v1.documents.StorageService.signed_url",
                return_value=fake_url,
            ):
                resp = client.get(
                    f"/api/v1/documents/{firm_owned_stored_document.id}",
                    follow_redirects=False,
                )
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)

        assert resp.status_code in (302, 307)
        assert resp.headers.get("location") == fake_url

    def test_download_allows_litigante_abogado_on_firm_owned_case(
        self, client, litigante_abogado, litigante_row, firm_owned_stored_document
    ):
        _client_as(client, str(litigante_abogado.id))
        try:
            with patch(
                "app.api.v1.documents.StorageService.retrieve",
                return_value=_FAKE_PDF,
            ):
                resp = client.get(
                    f"/api/v1/documents/{firm_owned_stored_document.id}/download"
                )
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)

        assert resp.status_code == 200
        assert resp.content == _FAKE_PDF

    def test_redirect_denies_outsider_lawyer_not_on_case(
        self, client, outsider_lawyer, firm_owned_stored_document
    ):
        _client_as(client, str(outsider_lawyer.id))
        try:
            resp = client.get(
                f"/api/v1/documents/{firm_owned_stored_document.id}",
                follow_redirects=False,
            )
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)

        assert resp.status_code == 404

    def test_download_denies_outsider_lawyer_not_on_case(
        self, client, outsider_lawyer, firm_owned_stored_document
    ):
        _client_as(client, str(outsider_lawyer.id))
        try:
            resp = client.get(
                f"/api/v1/documents/{firm_owned_stored_document.id}/download"
            )
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)

        assert resp.status_code == 404

    def test_download_allows_auditor_on_any_case(
        self, client, auditor_lawyer, firm_owned_stored_document
    ):
        _client_as(client, str(auditor_lawyer.id))
        try:
            with patch(
                "app.api.v1.documents.StorageService.retrieve",
                return_value=_FAKE_PDF,
            ):
                resp = client.get(
                    f"/api/v1/documents/{firm_owned_stored_document.id}/download"
                )
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)

        assert resp.status_code == 200
        assert resp.content == _FAKE_PDF

    def test_download_allows_admin_on_any_case(
        self, client, admin_lawyer, firm_owned_stored_document
    ):
        _client_as(client, str(admin_lawyer.id))
        try:
            with patch(
                "app.api.v1.documents.StorageService.retrieve",
                return_value=_FAKE_PDF,
            ):
                resp = client.get(
                    f"/api/v1/documents/{firm_owned_stored_document.id}/download"
                )
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)

        assert resp.status_code == 200
        assert resp.content == _FAKE_PDF

    def test_download_unknown_document_still_404(self, client, litigante_abogado):
        _client_as(client, str(litigante_abogado.id))
        try:
            resp = client.get("/api/v1/documents/999999/download")
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)

        assert resp.status_code == 404
