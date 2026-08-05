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


def _make_doc(db, case, texto, filename, doc_type="resolution"):
    obj = Document(
        case_id=case.id,
        doc_type=doc_type,
        gcs_path=f"cases/{case.id}/{filename}",
        status="stored",
        filename=filename,
        texto=texto,
        text_extracted_at=datetime.utcnow() if texto is not None else None,
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
