"""Tests for the document full-text search endpoint (Slice 1).

    GET /api/v1/documents/search

Runs on SQLite (the ``LIKE`` fallback path) — the PostgreSQL ``to_tsvector``
branch is exercised in prod. Verifies matching, the ``case_id`` filter, empty
results, ``q`` length validation, auth, and the response shape (snippet +
download_url).
"""
import pytest
from datetime import datetime

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.models.movement import Movement


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="55555555-5", name="Search Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T-SRCH", name="Juzgado Search", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_case(db, lawyer, court, rol):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
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


def _make_doc(
    db,
    case,
    texto,
    filename,
    doc_type="resolution",
    document_date=None,
    movement_id=None,
):
    obj = Document(
        case_id=case.id,
        doc_type=doc_type,
        gcs_path=f"cases/{case.id}/{filename}",
        status="stored",
        filename=filename,
        texto=texto,
        document_date=document_date,
        movement_id=movement_id,
        text_extracted_at=datetime.utcnow() if texto is not None else None,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_movement(db, case, movement_date, description="mov"):
    obj = Movement(
        case_id=case.id,
        description=description,
        movement_date=movement_date,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def case_a(db, lawyer, court):
    return _make_case(db, lawyer, court, "C-0001-2026")


@pytest.fixture
def case_b(db, lawyer, court):
    return _make_case(db, lawyer, court, "C-0002-2026")


@pytest.fixture
def docs(db, case_a, case_b):
    """Two cases, three docs with text + one doc with no extracted text."""
    d1 = _make_doc(
        db,
        case_a,
        "Se acoge la excepcion de prescripcion extintiva del deudor ejecutado.",
        "prescripcion_a.pdf",
    )
    d2 = _make_doc(
        db,
        case_a,
        "Notificacion de demanda por pagare en juicio ejecutivo.",
        "demanda_a.pdf",
    )
    d3 = _make_doc(
        db,
        case_b,
        "Resolucion que declara la prescripcion en otra causa distinta.",
        "prescripcion_b.pdf",
    )
    # A stored doc with NO extracted text — must never appear in results.
    d_null = _make_doc(db, case_a, None, "sin_texto_a.pdf")
    return {"d1": d1, "d2": d2, "d3": d3, "d_null": d_null}


@pytest.fixture
def authed_client(client, lawyer):
    async def _mock_lawyer():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock_lawyer
    yield client
    app.dependency_overrides.pop(get_current_lawyer, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDocumentSearch:
    def test_search_returns_matching_documents(self, authed_client, docs):
        resp = authed_client.get("/api/v1/documents/search", params={"q": "prescripcion"})
        assert resp.status_code == 200
        ids = {hit["document_id"] for hit in resp.json()}
        # d1 (case_a) and d3 (case_b) mention prescripcion; d2 does not.
        assert ids == {docs["d1"].id, docs["d3"].id}

    def test_case_id_filter_narrows_results(self, authed_client, docs, case_a):
        resp = authed_client.get(
            "/api/v1/documents/search",
            params={"q": "prescripcion", "case_id": case_a.id},
        )
        assert resp.status_code == 200
        ids = {hit["document_id"] for hit in resp.json()}
        assert ids == {docs["d1"].id}  # d3 lives in case_b → filtered out

    def test_non_matching_term_returns_empty(self, authed_client, docs):
        resp = authed_client.get(
            "/api/v1/documents/search", params={"q": "zzznoexistente"}
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_doc_without_text_is_never_returned(self, authed_client, docs):
        # "sin_texto" is part of the null-text doc's filename, not its texto.
        resp = authed_client.get(
            "/api/v1/documents/search", params={"q": "sin_texto"}
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_short_query_is_rejected(self, authed_client, docs):
        resp = authed_client.get("/api/v1/documents/search", params={"q": "a"})
        assert resp.status_code == 422

    def test_auth_required(self, client, docs):
        # No get_current_lawyer override and no Authorization header → 401.
        resp = client.get("/api/v1/documents/search", params={"q": "prescripcion"})
        assert resp.status_code == 401

    def test_response_shape_snippet_and_download_url(self, authed_client, docs):
        resp = authed_client.get(
            "/api/v1/documents/search",
            params={"q": "prescripcion", "case_id": docs["d1"].case_id},
        )
        assert resp.status_code == 200
        hit = resp.json()[0]
        assert hit["document_id"] == docs["d1"].id
        assert hit["case_id"] == docs["d1"].case_id
        assert hit["case_rol"] == "C-0001-2026"
        assert hit["doc_type"] == "resolution"
        assert hit["filename"] == "prescripcion_a.pdf"
        assert hit["snippet"]  # non-empty
        assert "prescripcion" in hit["snippet"].lower()
        assert hit["download_url"] == f"/api/v1/documents/{docs['d1'].id}/download"

    def test_limit_is_capped(self, authed_client, docs):
        resp = authed_client.get(
            "/api/v1/documents/search", params={"q": "prescripcion", "limit": 500}
        )
        assert resp.status_code == 422  # over the 100 cap

    def test_results_include_fecha_field(self, authed_client, db, case_a):
        # Doc with its own document_date.
        doc_dd = _make_doc(
            db,
            case_a,
            "Sentencia con fecha propia sobre la prescripcion.",
            "con_fecha.pdf",
            document_date=datetime(2026, 3, 15),
        )
        # Doc whose date comes from the linked movement.
        mov = _make_movement(db, case_a, datetime(2026, 2, 1))
        doc_mov = _make_doc(
            db,
            case_a,
            "Resolucion sobre la prescripcion vinculada a un movimiento.",
            "con_mov.pdf",
            movement_id=mov.id,
        )
        # Doc with neither → fecha is null.
        doc_none = _make_doc(
            db,
            case_a,
            "Escrito de prescripcion sin fecha alguna.",
            "sin_fecha.pdf",
        )

        resp = authed_client.get(
            "/api/v1/documents/search",
            params={"q": "prescripcion", "case_id": case_a.id},
        )
        assert resp.status_code == 200
        by_id = {hit["document_id"]: hit for hit in resp.json()}

        assert "fecha" in by_id[doc_dd.id]
        assert by_id[doc_dd.id]["fecha"] == "2026-03-15"
        assert by_id[doc_mov.id]["fecha"] == "2026-02-01"
        assert by_id[doc_none.id]["fecha"] is None

    def test_orden_reciente_sorts_newest_first_nulls_last(
        self, authed_client, db, case_a
    ):
        # Oldest via document_date.
        d_old = _make_doc(
            db,
            case_a,
            "Prescripcion antigua del deudor.",
            "old.pdf",
            document_date=datetime(2025, 1, 1),
        )
        # Newest via a linked movement's movement_date.
        mov = _make_movement(db, case_a, datetime(2026, 6, 30))
        d_new = _make_doc(
            db,
            case_a,
            "Prescripcion reciente del deudor.",
            "new.pdf",
            movement_id=mov.id,
        )
        # No date at all → must sort last.
        d_null = _make_doc(
            db,
            case_a,
            "Prescripcion sin fecha del deudor.",
            "null.pdf",
        )

        resp = authed_client.get(
            "/api/v1/documents/search",
            params={"q": "prescripcion", "case_id": case_a.id, "orden": "reciente"},
        )
        assert resp.status_code == 200
        order = [hit["document_id"] for hit in resp.json()]
        assert order == [d_new.id, d_old.id, d_null.id]

    def test_orden_invalid_falls_back_to_relevancia(self, authed_client, docs):
        # An unknown orden value must not error — clamped to relevancia.
        resp = authed_client.get(
            "/api/v1/documents/search",
            params={"q": "prescripcion", "orden": "bogus"},
        )
        assert resp.status_code == 200
        ids = {hit["document_id"] for hit in resp.json()}
        assert ids == {docs["d1"].id, docs["d3"].id}
