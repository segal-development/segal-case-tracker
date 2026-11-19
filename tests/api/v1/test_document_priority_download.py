"""GET /api/v1/documents/{id}/download — priority-download request (S2, S3, S5).

A document that is not stored is never downloaded from PJUD by the API: the
request marks it for priority download (the station does the PJUD work) and
answers 202. No network, no Playwright, no Redis.
"""
from datetime import datetime
from unittest.mock import patch

import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer

_FAKE_PDF = b"%PDF-1.4 fake"
_NO_TOKEN_MSG = "Document has no PJUD token — re-sync the case first"


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="55555555-5", name="Prio Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def case_obj(db, lawyer):
    court = Court(code="T-PRIO", name="Juzgado Prio", region="RM", type="civil")
    db.add(court)
    db.commit()
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-0077-2026",
        status="active",
        competencia="civil",
        plaintiff="DTE",
        defendant="DDO",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _doc(db, case, **kw):
    fields = {
        "case_id": case.id,
        "doc_type": "resolution",
        "pjud_endpoint": "documentos/docuS.php",
        "pjud_token": "eyJhbGciOiJub25lIn0.e30.TOK",
        "status": "pending",
    }
    fields.update(kw)
    obj = Document(**fields)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def authed_client(client, lawyer):
    async def _me():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _me
    yield client
    app.dependency_overrides.pop(get_current_lawyer, None)


class TestStoredUnchanged:
    def test_stored_document_still_streams(self, authed_client, db, case_obj):
        doc = _doc(db, case_obj, status="stored", gcs_path="cases/1/x.pdf",
                   filename="x.pdf")
        with patch("app.api.v1.documents.StorageService.retrieve",
                   return_value=_FAKE_PDF):
            resp = authed_client.get(f"/api/v1/documents/{doc.id}/download")
        assert resp.status_code == 200
        assert resp.content == _FAKE_PDF
        db.refresh(doc)
        assert doc.prioridad_solicitada_at is None


class TestPendingRequestsPriority:
    def test_pending_returns_202_and_sets_timestamp(self, authed_client, db, case_obj):
        doc = _doc(db, case_obj)
        before = datetime.utcnow()
        resp = authed_client.get(f"/api/v1/documents/{doc.id}/download")
        assert resp.status_code == 202
        body = resp.json()
        assert body["estado"] == "en_descarga"
        assert body["detail"] == "Documento en cola prioritaria de descarga"
        db.refresh(doc)
        assert doc.prioridad_solicitada_at is not None
        assert doc.prioridad_solicitada_at >= before
        assert doc.status == "pending"
        assert body["solicitado_at"] == doc.prioridad_solicitada_at.isoformat()

    def test_second_call_keeps_first_timestamp(self, authed_client, db, case_obj):
        doc = _doc(db, case_obj)
        first = authed_client.get(f"/api/v1/documents/{doc.id}/download").json()
        second = authed_client.get(f"/api/v1/documents/{doc.id}/download")
        assert second.status_code == 202
        assert second.json()["solicitado_at"] == first["solicitado_at"]
        db.refresh(doc)
        assert doc.prioridad_solicitada_at.isoformat() == first["solicitado_at"]

    def test_never_touches_pjud(self, authed_client, db, case_obj):
        doc = _doc(db, case_obj)
        import app.api.v1.documents as mod

        for name in ("BrowserFactory", "CivilScraper", "get_session_store",
                     "download_with_budget"):
            assert not hasattr(mod, name), f"{name} still imported in the API"
        assert authed_client.get(
            f"/api/v1/documents/{doc.id}/download"
        ).status_code == 202

    def test_failed_is_requeued_once(self, authed_client, db, case_obj):
        doc = _doc(db, case_obj, status="failed", failed_at=datetime.utcnow())
        resp = authed_client.get(f"/api/v1/documents/{doc.id}/download")
        assert resp.status_code == 202
        db.refresh(doc)
        assert doc.status == "pending"
        assert doc.prioridad_solicitada_at is not None
        first = doc.prioridad_solicitada_at
        # The downloader fails it again; a second request must NOT requeue it.
        doc.status = "failed"
        db.commit()
        resp2 = authed_client.get(f"/api/v1/documents/{doc.id}/download")
        assert resp2.status_code == 202
        db.refresh(doc)
        assert doc.status == "failed"
        assert doc.prioridad_solicitada_at == first


class TestNoTokenAndUnavailable:
    def test_no_token_returns_409_sin_token(self, authed_client, db, case_obj):
        doc = _doc(db, case_obj, pjud_token=None)
        resp = authed_client.get(f"/api/v1/documents/{doc.id}/download")
        assert resp.status_code == 409
        assert resp.json() == {"estado": "sin_token", "detail": _NO_TOKEN_MSG}
        db.refresh(doc)
        assert doc.prioridad_solicitada_at is None

    def test_unavailable_returns_409_no_disponible(self, authed_client, db, case_obj):
        doc = _doc(db, case_obj, status="unavailable", pjud_token=None)
        resp = authed_client.get(f"/api/v1/documents/{doc.id}/download")
        assert resp.status_code == 409
        assert resp.json() == {
            "estado": "no_disponible",
            "detail": "Documento no disponible en el PJUD",
        }
        db.refresh(doc)
        assert doc.prioridad_solicitada_at is None


class TestVisibility:
    def test_out_of_scope_doc_404_and_nothing_written(self, client, db, case_obj):
        stranger = Lawyer(rut="44444444-4", name="Stranger")
        db.add(stranger)
        db.commit()
        doc = _doc(db, case_obj)

        async def _other():
            return {"sub": str(stranger.id)}

        app.dependency_overrides[get_current_lawyer] = _other
        try:
            resp = client.get(f"/api/v1/documents/{doc.id}/download")
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)
        assert resp.status_code == 404
        db.refresh(doc)
        assert doc.prioridad_solicitada_at is None
        assert doc.status == "pending"
