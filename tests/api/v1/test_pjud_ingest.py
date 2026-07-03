"""Tests for POST /api/v1/pjud/ingest/cases and POST /api/v1/pjud/ingest/movements.

Auth is via the X-Ingest-Key header (require_ingest_key), NOT the lawyer
JWT — these endpoints are called by the browser extension's service worker,
not by a logged-in lawyer.
"""

import hashlib

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.ingest_key import IngestKey
from app.models.lawyer import Lawyer
from app.models.movement import Movement

INGEST_URL = "/api/v1/pjud/ingest/cases"
MOVEMENTS_URL = "/api/v1/pjud/ingest/movements"


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
