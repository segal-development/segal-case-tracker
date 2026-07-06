"""Tests for POST /api/v1/pjud/ingest/cases and POST /api/v1/pjud/ingest/movements.

Auth is via the X-Ingest-Key header (require_ingest_key), NOT the lawyer
JWT — these endpoints are called by the browser extension's service worker,
not by a logged-in lawyer.
"""

import base64
import hashlib
from datetime import datetime

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.ingest_key import IngestKey
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.document_persistence import document_identity_hash

INGEST_URL = "/api/v1/pjud/ingest/cases"
MOVEMENTS_URL = "/api/v1/pjud/ingest/movements"
DOCUMENTS_URL = "/api/v1/pjud/ingest/documents"

FIRM_RUT = "16021492-9"


@pytest.fixture(autouse=True)
def _seed_firm_lawyer(db):
    """Seed the firm's canonical Lawyer row (FIRM_LAWYER_RUT default).

    Approach C (unificar-modelo-causas, PR1b): ingest_cases upserts Case rows
    under the firm lawyer_id, not the syncing lawyer's own id, so
    firm_lawyer_id(db) must resolve for every ingest endpoint test here.
    """
    existing = db.query(Lawyer).filter(Lawyer.rut == FIRM_RUT).first()
    if existing is None:
        db.add(Lawyer(rut=FIRM_RUT, name="Firm Lawyer", is_active=True))
        db.commit()


def _case_row(token: str, rol: str, tribunal: str, caratulado: str) -> str:
    return f"""
    <tr>
      <td><a onclick="detalleMisCausaCivil('{token}')">Ver</a></td>
      <td>{rol}</td>
      <td>{tribunal}</td>
      <td>{caratulado}</td>
      <td>01/01/2026</td>
      <td>En tramitacion</td>
      <td>Principal</td>
      <td>-</td>
    </tr>
    """


def _mis_causas_page(rows: list[str]) -> str:
    return f"""
    <html><body>
      Total de registros: {len(rows)}
      <table>{''.join(rows)}</table>
    </body></html>
    """


VALID_PAGE = _mis_causas_page(
    [_case_row("TOKEN1", "C-1234-2026", "1 Juzgado Civil de Santiago", "BANCO DE CHILE / PEREZ")]
)
MALFORMED_PAGE = "<html><body>Not a PJUD page.</body></html>"


@pytest.fixture
def ingest_key(db) -> str:
    """Create an active IngestKey and return its plaintext value."""
    plaintext = "test-ingest-key-plaintext"
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    db.add(IngestKey(label="operator-test", key_hash=key_hash, is_active=True))
    db.commit()
    return plaintext


class TestIngestCasesEndpointAuth:
    def test_missing_key_returns_401(self, client):
        response = client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [VALID_PAGE]},
        )
        assert response.status_code == 401

    def test_invalid_key_returns_403(self, client, ingest_key):
        response = client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [VALID_PAGE]},
            headers={"X-Ingest-Key": "wrong-key"},
        )
        assert response.status_code == 403


class TestIngestCasesEndpointHappyPath:
    def test_valid_payload_returns_counts(self, client, ingest_key):
        response = client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [VALID_PAGE]},
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["new"] == 1
        assert data["existing"] == 0
        assert data["errors"] == []

    def test_malformed_html_returns_4xx_and_no_500(self, client, ingest_key):
        response = client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [MALFORMED_PAGE]},
            headers={"X-Ingest-Key": ingest_key},
        )
        assert 400 <= response.status_code < 500

    def test_repeat_ingest_returns_zero_new(self, client, ingest_key):
        client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [VALID_PAGE]},
            headers={"X-Ingest-Key": ingest_key},
        )
        response = client.post(
            INGEST_URL,
            json={"rut": "11111111-1", "competencia": "civil", "pages": [VALID_PAGE]},
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["new"] == 0
        assert data["existing"] == 1


# ---------------------------------------------------------------------------
# POST /pjud/ingest/movements
# ---------------------------------------------------------------------------


def _detail_html(rol: str, folio: str, descripcion: str, fecha: str) -> str:
    return f"""
    <html><body>
      <table>
        <tr><td><strong>ROL:</strong> {rol}</td></tr>
        <tr><td><strong>Tribunal:</strong> 1 Juzgado Civil de Santiago</td></tr>
      </table>
      <div id="historiaCiv">
        <div class="panel panel-default">
          <div class="table-responsive">
            <table class="table table-bordered table-striped table-hover">
              <thead><tr><th>Folio</th><th>Doc.</th><th>Anexo</th><th>Etapa</th>
              <th>Tramite</th><th>Desc.</th><th>Fecha</th><th>Foja</th><th>Geo</th></tr></thead>
              <tbody>
                <tr>
                  <td>{folio}</td>
                  <td align="left"></td>
                  <td align="center"></td>
                  <td>En tramitacion</td>
                  <td>Resolucion</td>
                  <td></td>
                  <td>{descripcion}</td>
                  <td>{fecha}</td>
                  <td>1</td>
                  <td></td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </body></html>
    """


MOVEMENTS_MALFORMED_HTML = "<html><body>Not a PJUD detail page.</body></html>"


@pytest.fixture
def seeded_case(db):
    """Lawyer + Court + Case pre-seeded for movements ingest tests."""
    lawyer = Lawyer(rut="11111111-1", name="Test Lawyer", is_active=True)
    db.add(lawyer)
    db.flush()

    court = Court(code="T1-MOV", name="Juzgado Movements Test", region="RM", type="civil")
    db.add(court)
    db.flush()

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-1234-2026",
        competencia="civil",
        status="active",
    )
    db.add(case)
    db.commit()
    return {"lawyer": lawyer, "case": case}


class TestIngestMovementsEndpointAuth:
    def test_missing_key_returns_401(self, client, seeded_case):
        response = client.post(
            MOVEMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "cases": [
                    {
                        "rol": "C-1234-2026",
                        "html": _detail_html("C-1234-2026", "1", "Se dicta resolucion", "15/01/2026"),
                    }
                ],
            },
        )
        assert response.status_code == 401

    def test_invalid_key_returns_403(self, client, ingest_key, seeded_case):
        response = client.post(
            MOVEMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "cases": [
                    {
                        "rol": "C-1234-2026",
                        "html": _detail_html("C-1234-2026", "1", "Se dicta resolucion", "15/01/2026"),
                    }
                ],
            },
            headers={"X-Ingest-Key": "wrong-key"},
        )
        assert response.status_code == 403


class TestIngestMovementsEndpointHappyPath:
    def test_valid_detail_html_persists_movements_and_classifies(
        self, client, ingest_key, seeded_case, db
    ):
        response = client.post(
            MOVEMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "cases": [
                    {
                        "rol": "C-1234-2026",
                        "html": _detail_html("C-1234-2026", "1", "Se dicta resolucion", "15/01/2026"),
                    }
                ],
            },
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["cases_processed"] == 1
        assert data["movements_new"] == 1
        assert data["classified"] == 1
        assert data["errors"] == []

        movements = db.query(Movement).all()
        assert len(movements) == 1
        assert movements[0].description == "Se dicta resolucion"

        case = db.query(Case).filter(Case.rol == "C-1234-2026").first()
        assert case.last_detail_checked_at is not None
        assert case.semaforo is not None

    def test_unknown_rol_is_skipped_with_error(self, client, ingest_key, seeded_case):
        response = client.post(
            MOVEMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "cases": [
                    {
                        "rol": "C-9999-2026",
                        "html": _detail_html("C-9999-2026", "1", "Se dicta resolucion", "15/01/2026"),
                    }
                ],
            },
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["cases_processed"] == 0
        assert len(data["errors"]) == 1

    def test_forwards_failed_rols_and_stamps_them(self, client, ingest_key, seeded_case, db):
        """A failed-only POST (no successful cases) stamps clearly-OLD ROLs so
        they rotate out of subsequent batches. Recent ROLs are protected by the
        systemic-failure guard (a RUT/session mismatch surfaces recent ROLs) —
        covered in the service-level tests."""
        lawyer = db.query(Lawyer).filter(Lawyer.rut == "11111111-1").first()
        court = db.query(Court).first()
        db.add(Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-100-2006",
                    competencia="civil", status="active"))
        db.commit()

        response = client.post(
            MOVEMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "cases": [],
                "failed_rols": ["C-100-2006"],
            },
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["failed_stamped"] == 1
        assert data["cases_processed"] == 0

        case = db.query(Case).filter(Case.rol == "C-100-2006").first()
        assert case.last_detail_checked_at is not None

    def test_malformed_html_is_graceful_no_500(self, client, ingest_key, seeded_case):
        response = client.post(
            MOVEMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "cases": [{"rol": "C-1234-2026", "html": MOVEMENTS_MALFORMED_HTML}],
            },
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["cases_processed"] == 0
        assert len(data["errors"]) == 1


# ---------------------------------------------------------------------------
# POST /pjud/ingest/documents (Slice 3)
# ---------------------------------------------------------------------------

_FAKE_PDF = b"%PDF-1.4 fake pdf bytes for test"


@pytest.fixture
def pending_document(db, seeded_case):
    """Insert a pending Document tied to the seeded case, return its token_hash."""
    case = seeded_case["case"]
    token_hash = document_identity_hash("resolution", case.rol, "1")
    doc = Document(
        case_id=case.id,
        doc_type="resolution",
        pjud_endpoint="documentos/docuS.php",
        pjud_token_hash=token_hash,
        status="pending",
    )
    db.add(doc)
    db.commit()
    return token_hash


@pytest.fixture
def seeded_movement(db, seeded_case):
    """A Movement on the seeded case (folio '5') for movement-level doc tests."""
    case = seeded_case["case"]
    mv = Movement(
        case_id=case.id,
        description="Resolucion folio 5",
        folio="5",
        movement_date=datetime(2026, 1, 15),
    )
    db.add(mv)
    db.commit()
    db.refresh(mv)
    return mv


class TestIngestDocumentsEndpointAuth:
    def test_missing_key_returns_401(self, client, seeded_case):
        response = client.post(
            DOCUMENTS_URL,
            json={"rut": "11111111-1", "competencia": "civil", "documents": []},
        )
        assert response.status_code == 401


class TestIngestDocumentsEndpointHappyPath:
    def test_updates_existing_pending_document_and_stores(
        self, client, ingest_key, seeded_case, pending_document, db, tmp_path, monkeypatch
    ):
        """(b) POST matching an existing pending row → updated + stored."""
        from app.config import settings

        monkeypatch.setattr(settings, "DOC_STORAGE_DIR", str(tmp_path))

        response = client.post(
            DOCUMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "documents": [
                    {
                        "rol": "C-1234-2026",
                        "token_hash": pending_document,
                        "doc_type": "resolution",
                        "scope_key": "1",
                        "filename": "resolucion.pdf",
                        "content_type": "application/pdf",
                        "document_date": "15/01/2026",
                        "base64": base64.b64encode(_FAKE_PDF).decode(),
                    }
                ],
            },
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["documents_stored"] == 1
        assert data["documents_created"] == 0
        assert data["skipped"] == 0
        assert data["errors"] == []

        # No duplicate row created — updated in place.
        assert db.query(Document).filter(
            Document.pjud_token_hash == pending_document
        ).count() == 1
        doc = db.query(Document).filter(
            Document.pjud_token_hash == pending_document
        ).first()
        assert doc.status == "stored"
        assert doc.filename == "resolucion.pdf"
        assert doc.content_type == "application/pdf"
        assert doc.size_bytes == len(_FAKE_PDF)
        assert doc.gcs_path

    def test_creates_case_level_document_when_none_exists(
        self, client, ingest_key, seeded_case, db, tmp_path, monkeypatch
    ):
        """(a) POST with a token_hash that has NO existing row → CREATED + stored."""
        from app.config import settings

        monkeypatch.setattr(settings, "DOC_STORAGE_DIR", str(tmp_path))
        case = seeded_case["case"]
        token_hash = document_identity_hash("cert_envio", case.rol, "")

        response = client.post(
            DOCUMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "documents": [
                    {
                        "rol": "C-1234-2026",
                        "token_hash": token_hash,
                        "doc_type": "cert_envio",
                        "scope_key": "",
                        "pjud_endpoint": "documentos/docuS.php",
                        "pjud_token": "live.jwt.token",
                        "filename": "cert.pdf",
                        "content_type": "application/pdf",
                        "document_date": "15/01/2026",
                        "base64": base64.b64encode(_FAKE_PDF).decode(),
                    }
                ],
            },
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["documents_stored"] == 1
        assert data["documents_created"] == 1
        assert data["skipped"] == 0
        assert data["errors"] == []

        doc = db.query(Document).filter(
            Document.pjud_token_hash == token_hash
        ).first()
        assert doc is not None
        assert doc.status == "stored"
        assert doc.doc_type == "cert_envio"
        assert doc.case_id == case.id
        assert doc.movement_id is None
        assert doc.size_bytes == len(_FAKE_PDF)
        assert doc.gcs_path
        assert doc.pjud_endpoint == "documentos/docuS.php"
        assert doc.pjud_token == "live.jwt.token"

    def test_creates_movement_level_document_resolves_movement_id(
        self, client, ingest_key, seeded_case, seeded_movement, db, tmp_path, monkeypatch
    ):
        """(a) A movement-scoped create resolves movement_id via (case_id, folio)."""
        from app.config import settings

        monkeypatch.setattr(settings, "DOC_STORAGE_DIR", str(tmp_path))
        case = seeded_case["case"]
        token_hash = document_identity_hash("resolution", case.rol, "5")

        response = client.post(
            DOCUMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "documents": [
                    {
                        "rol": "C-1234-2026",
                        "token_hash": token_hash,
                        "doc_type": "resolution",
                        "scope_key": "5",
                        "filename": "reso.pdf",
                        "content_type": "application/pdf",
                        "base64": base64.b64encode(_FAKE_PDF).decode(),
                    }
                ],
            },
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["documents_stored"] == 1
        assert data["documents_created"] == 1

        doc = db.query(Document).filter(
            Document.pjud_token_hash == token_hash
        ).first()
        assert doc is not None
        assert doc.movement_id == seeded_movement.id
        assert doc.status == "stored"

    def test_idempotent_recreate_does_not_duplicate(
        self, client, ingest_key, seeded_case, db, tmp_path, monkeypatch
    ):
        """Re-POST of the same token_hash must not create a second row."""
        from app.config import settings

        monkeypatch.setattr(settings, "DOC_STORAGE_DIR", str(tmp_path))
        case = seeded_case["case"]
        token_hash = document_identity_hash("cert_envio", case.rol, "")
        payload = {
            "rut": "11111111-1",
            "competencia": "civil",
            "documents": [
                {
                    "rol": "C-1234-2026",
                    "token_hash": token_hash,
                    "doc_type": "cert_envio",
                    "scope_key": "",
                    "filename": "cert.pdf",
                    "content_type": "application/pdf",
                    "base64": base64.b64encode(_FAKE_PDF).decode(),
                }
            ],
        }
        first = client.post(
            DOCUMENTS_URL, json=payload, headers={"X-Ingest-Key": ingest_key}
        )
        second = client.post(
            DOCUMENTS_URL, json=payload, headers={"X-Ingest-Key": ingest_key}
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["documents_created"] == 1
        assert second.json()["documents_created"] == 0
        assert second.json()["documents_stored"] == 1
        assert db.query(Document).filter(
            Document.pjud_token_hash == token_hash
        ).count() == 1

    def test_mismatched_hash_still_stored_no_raise(
        self, client, ingest_key, seeded_case, db, tmp_path, monkeypatch
    ):
        """(c) token_hash that does NOT equal the identity formula still stores
        (defensive) and never raises."""
        from app.config import settings

        monkeypatch.setattr(settings, "DOC_STORAGE_DIR", str(tmp_path))
        case = seeded_case["case"]
        # 64 hex chars that do NOT equal sha256(doc_type|rol|scope_key).
        bogus_hash = "abc123" * 10 + "abcd"
        assert len(bogus_hash) == 64
        assert bogus_hash != document_identity_hash("resolution", case.rol, "1")

        response = client.post(
            DOCUMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "documents": [
                    {
                        "rol": "C-1234-2026",
                        "token_hash": bogus_hash,
                        "doc_type": "resolution",
                        "scope_key": "1",
                        "filename": "mismatch.pdf",
                        "content_type": "application/pdf",
                        "base64": base64.b64encode(_FAKE_PDF).decode(),
                    }
                ],
            },
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["documents_stored"] == 1
        assert data["skipped"] == 0

        doc = db.query(Document).filter(
            Document.pjud_token_hash == bogus_hash
        ).first()
        assert doc is not None
        assert doc.status == "stored"

    def test_unknown_case_rol_is_skipped(
        self, client, ingest_key, seeded_case, db, tmp_path, monkeypatch
    ):
        """A ROL that is not this lawyer's case is skipped, no row created."""
        from app.config import settings

        monkeypatch.setattr(settings, "DOC_STORAGE_DIR", str(tmp_path))

        response = client.post(
            DOCUMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "documents": [
                    {
                        "rol": "C-9999-2026",
                        "token_hash": "deadbeef" * 8,
                        "doc_type": "resolution",
                        "scope_key": "1",
                        "filename": "ghost.pdf",
                        "content_type": "application/pdf",
                        "base64": base64.b64encode(_FAKE_PDF).decode(),
                    }
                ],
            },
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["documents_stored"] == 0
        assert data["skipped"] == 1
        assert len(data["errors"]) == 1
        assert db.query(Document).count() == 0

    def test_invalid_base64_is_skipped_no_raise(
        self, client, ingest_key, seeded_case, pending_document, db, tmp_path, monkeypatch
    ):
        from app.config import settings

        monkeypatch.setattr(settings, "DOC_STORAGE_DIR", str(tmp_path))

        response = client.post(
            DOCUMENTS_URL,
            json={
                "rut": "11111111-1",
                "competencia": "civil",
                "documents": [
                    {
                        "rol": "C-1234-2026",
                        "token_hash": pending_document,
                        "doc_type": "resolution",
                        "scope_key": "1",
                        "filename": "broken.pdf",
                        "content_type": "application/pdf",
                        "document_date": None,
                        "base64": "not-valid-base64!!!",
                    }
                ],
            },
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["documents_stored"] == 0
        assert data["skipped"] == 1

        doc = db.query(Document).filter(
            Document.pjud_token_hash == pending_document
        ).first()
        assert doc.status == "pending"
