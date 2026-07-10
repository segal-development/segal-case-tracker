"""Tests for generated-document persistence (system requirement #3, slice 2).

Slice 1 streamed the escrito .docx WITHOUT persisting. Slice 2 also stores a
``GeneratedDocument`` row + the bytes in the storage backend, and surfaces the
generation in the case timeline (kind="documento_generado"). The streaming
response behavior must stay identical to slice 1.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.generated_document import GeneratedDocument
from app.models.lawyer import Lawyer
from app.services.storage_service import get_storage_backend
from app.services.timeline_service import map_generated_document

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
AUDITOR_RUT = "77777777-7"


@pytest.fixture
def temp_storage(tmp_path, monkeypatch):
    """Point the local storage backend at a throwaway temp dir for each test."""
    monkeypatch.setattr(settings, "DOC_STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "DOC_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def auditor(db):
    obj = Lawyer(rut=AUDITOR_RUT, name="Auditor", role="auditor")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-DOCPERSIST", name="Juzgado Persist", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def case(db, auditor, court):
    obj = Case(
        lawyer_id=auditor.id,
        court_id=court.id,
        rol="C-4502-2025",
        status="active",
        competencia="civil",
        plaintiff="Banco Ejemplo S.A.",
        defendant="Juan Pérez",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj); db.commit(); db.refresh(obj)
    for i, (part, rut, nombre) in enumerate([
        ("DTE.", "60000000-0", "Banco Ejemplo S.A."),
        ("DDO.", "12345678-9", "Juan Pérez"),
    ]):
        db.add(CaseLitigante(
            case_id=obj.id, participante=part, rut=rut,
            persona_type="NATURAL", nombre=nombre, natural_key=f"{obj.id}-{i}",
        ))
    db.commit()
    return obj


def _headers(rut: str) -> dict:
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


class TestGeneratePersists:
    def test_persists_generated_document_row(self, temp_storage, db, client, case, auditor):
        resp = client.post(
            f"/api/v1/cases/{case.id}/documents/generate",
            json={"document_type": "escrito_oposicion"},
            headers=_headers(AUDITOR_RUT),
        )
        # Streaming response is unchanged.
        assert resp.status_code == 200
        assert resp.headers["content-type"] == DOCX_MEDIA_TYPE
        assert "attachment" in resp.headers["content-disposition"]
        assert resp.content[:2] == b"PK"

        rows = db.query(GeneratedDocument).filter(
            GeneratedDocument.case_id == case.id
        ).all()
        assert len(rows) == 1
        gd = rows[0]
        assert gd.case_id == case.id
        assert gd.document_type == "escrito_oposicion"
        assert gd.generated_by_rut == AUDITOR_RUT
        assert gd.content_type == DOCX_MEDIA_TYPE
        assert gd.size_bytes and gd.size_bytes > 0
        assert gd.status == "stored"
        assert gd.storage_key is not None

    def test_stored_bytes_are_retrievable_and_are_a_docx(
        self, temp_storage, db, client, case, auditor
    ):
        resp = client.post(
            f"/api/v1/cases/{case.id}/documents/generate",
            json={"document_type": "escrito_oposicion"},
            headers=_headers(AUDITOR_RUT),
        )
        assert resp.status_code == 200

        gd = db.query(GeneratedDocument).filter(
            GeneratedDocument.case_id == case.id
        ).first()
        stored = get_storage_backend(settings).retrieve(gd.storage_key)
        assert stored[:2] == b"PK"
        assert len(stored) == gd.size_bytes

    def test_timeline_includes_generated_document(
        self, temp_storage, db, client, case, auditor
    ):
        client.post(
            f"/api/v1/cases/{case.id}/documents/generate",
            json={"document_type": "escrito_oposicion"},
            headers=_headers(AUDITOR_RUT),
        )
        gd = db.query(GeneratedDocument).filter(
            GeneratedDocument.case_id == case.id
        ).first()

        resp = client.get(
            f"/api/v1/cases/{case.id}/timeline", headers=_headers(AUDITOR_RUT)
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        generated = [it for it in items if it["kind"] == "documento_generado"]
        assert len(generated) == 1
        assert generated[0]["ref_id"] == gd.id

    def test_refusal_path_persists_nothing(self, temp_storage, db, client, case, auditor):
        # Firm is the creditor's abogado (AB.DTE) → endpoint refuses with 409
        # and must NOT create a GeneratedDocument row.
        db.add(CaseLitigante(
            case_id=case.id, participante="AB.DTE", rut=AUDITOR_RUT,
            persona_type="NATURAL", nombre="Auditor", natural_key=f"{case.id}-abdte",
        ))
        db.commit()

        resp = client.post(
            f"/api/v1/cases/{case.id}/documents/generate",
            json={"document_type": "escrito_oposicion"},
            headers=_headers(AUDITOR_RUT),
        )
        assert resp.status_code == 409
        rows = db.query(GeneratedDocument).filter(
            GeneratedDocument.case_id == case.id
        ).all()
        assert rows == []


class TestMapGeneratedDocument:
    def test_maps_to_timeline_event_shape(self):
        gd = SimpleNamespace(
            id=42,
            document_type="escrito_oposicion",
            filename="oposicion_excepciones_C_4502_2025.docx",
            generated_at=datetime(2026, 7, 10, 12, 0, 0),
            generated_by_name="María González",
            status="stored",
        )
        event = map_generated_document(gd)
        assert event.kind == "documento_generado"
        assert event.date == datetime(2026, 7, 10, 12, 0, 0)
        assert event.title == "Escrito de oposición de excepciones"
        assert event.description == "Generado por María González"
        assert event.ref_id == 42
        assert event.status == "stored"

    def test_unknown_type_falls_back_to_filename_then_type(self):
        gd = SimpleNamespace(
            id=7,
            document_type="otro_escrito",
            filename="algo.docx",
            generated_at=datetime(2026, 7, 10, 12, 0, 0),
            generated_by_name=None,
            status="stored",
        )
        event = map_generated_document(gd)
        assert event.title == "algo.docx"
        assert event.description is None
