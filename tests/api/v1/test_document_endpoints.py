"""Tests for document list and download endpoints.

Endpoints under test:
    GET /api/v1/cases/{case_id}/documents
    GET /api/v1/documents/{document_id}/download

Strategy: mock the scraper download method and the session store — no network
calls, no Playwright, no Redis.
"""

import pytest
from datetime import datetime


from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="22222222-2", name="Doc Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T-DOC", name="Juzgado Docs", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def case(db, lawyer, court):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-9999-2025",
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
def document(db, case):
    """A document with a valid token and endpoint."""
    obj = Document(
        case_id=case.id,
        doc_type="resolution",
        pjud_endpoint="documentos/docuS.php",
        pjud_token="eyJhbGciOiJIUzI1NiJ9.FAKE.TOKEN",
        filename="resolucion_001.pdf",
        status="pending",
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def unavailable_document(db, case):
    """A document marked as unavailable (fa-ban)."""
    obj = Document(
        case_id=case.id,
        doc_type="escrito_cert",
        pjud_endpoint="documentos/docCertificadoDemanda.php",
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

    async def _mock_get_current_lawyer():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock_get_current_lawyer
    yield client
    # dependency_overrides cleared by the parent `client` fixture


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/cases/{case_id}/documents
# ─────────────────────────────────────────────────────────────────────────────


class TestListCaseDocuments:
    def test_returns_empty_list_when_no_documents(self, authed_client, case):
        resp = authed_client.get(f"/api/v1/cases/{case.id}/documents")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_documents_for_case(
        self, authed_client, case, document, unavailable_document
    ):
        resp = authed_client.get(f"/api/v1/cases/{case.id}/documents")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2

        ids = {i["id"] for i in items}
        assert document.id in ids
        assert unavailable_document.id in ids

    def test_document_item_has_expected_fields(
        self, authed_client, case, document
    ):
        resp = authed_client.get(f"/api/v1/cases/{case.id}/documents")
        assert resp.status_code == 200
        item = next(i for i in resp.json() if i["id"] == document.id)

        assert item["doc_type"] == "resolution"
        assert item["pjud_endpoint"] == "documentos/docuS.php"
        assert item["filename"] == "resolucion_001.pdf"
        assert item["status"] == "pending"
        assert item["available"] is True
        assert item["download_url"] == f"/api/v1/documents/{document.id}/download"

    def test_movement_linked_doc_exposes_filing_date(self, authed_client, db, case):
        """A doc linked to a movement exposes that movement's date; a case-level
        static doc (no movement) has date None."""
        from app.models.movement import Movement

        mv = Movement(
            case_id=case.id,
            description="Resolución",
            movement_date=datetime(2026, 5, 29),
        )
        db.add(mv)
        db.commit()
        db.refresh(mv)
        linked = Document(
            case_id=case.id, doc_type="resolution", status="stored", movement_id=mv.id
        )
        static = Document(case_id=case.id, doc_type="texto_demanda", status="stored")
        db.add_all([linked, static])
        db.commit()
        db.refresh(linked)
        db.refresh(static)

        resp = authed_client.get(f"/api/v1/cases/{case.id}/documents")
        assert resp.status_code == 200
        by_id = {d["id"]: d for d in resp.json()}
        assert by_id[linked.id]["doc_date"] == "2026-05-29"
        assert by_id[static.id]["doc_date"] is None

    def test_unavailable_document_has_available_false(
        self, authed_client, case, unavailable_document
    ):
        resp = authed_client.get(f"/api/v1/cases/{case.id}/documents")
        items = resp.json()
        item = next(i for i in items if i["id"] == unavailable_document.id)
        assert item["available"] is False

    def test_returns_404_for_unknown_case(self, authed_client):
        resp = authed_client.get("/api/v1/cases/99999/documents")
        assert resp.status_code == 404

    def test_returns_404_for_case_owned_by_other_lawyer(
        self, client, db, case
    ):
        """A different lawyer cannot see another lawyer's case documents."""
        other = Lawyer(rut="33333333-3", name="Other Lawyer")
        db.add(other)
        db.commit()
        db.refresh(other)

        async def _other_lawyer():
            return {"sub": str(other.id)}

        app.dependency_overrides[get_current_lawyer] = _other_lawyer
        try:
            resp = client.get(f"/api/v1/cases/{case.id}/documents")
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/cases/{case_id}/documents — DATE ORDERING (regression)
# ─────────────────────────────────────────────────────────────────────────────


class TestListCaseDocumentsOrdering:
    """Regression: the list must be ordered by ``doc_date`` DESCENDING (newest
    first), with case-level docs that have no date (doc_date=None) sorting LAST.

    The seed data below is arranged so that DB/insertion (PK) order deliberately
    differs from date order: movements are inserted mid → oldest → newest, and a
    case-level (null-date) doc is inserted in the middle. Without the endpoint's
    sort, the response would come back in insertion order and this test fails.
    """

    def test_documents_ordered_by_date_desc_nulls_last(
        self, authed_client, db, case
    ):
        from app.models.movement import Movement

        # Insert movements OUT of chronological order: mid, then oldest, then
        # newest → PK/insertion order != date order.
        mv_mid = Movement(
            case_id=case.id,
            description="Resolución media",
            movement_date=datetime(2026, 3, 15),
        )
        mv_old = Movement(
            case_id=case.id,
            description="Resolución antigua",
            movement_date=datetime(2026, 1, 1),
        )
        mv_new = Movement(
            case_id=case.id,
            description="Resolución reciente",
            movement_date=datetime(2026, 6, 30),
        )
        db.add_all([mv_mid, mv_old, mv_new])
        db.commit()
        db.refresh(mv_mid)
        db.refresh(mv_old)
        db.refresh(mv_new)

        # One document per movement, inserted in the same skewed order, plus a
        # case-level (movement_id=None) doc dropped in the MIDDLE of insertion.
        doc_mid = Document(
            case_id=case.id,
            doc_type="resolution",
            status="stored",
            movement_id=mv_mid.id,
        )
        doc_old = Document(
            case_id=case.id,
            doc_type="resolution",
            status="stored",
            movement_id=mv_old.id,
        )
        doc_static = Document(
            case_id=case.id,
            doc_type="texto_demanda",
            status="stored",
        )  # case-level, no movement → doc_date is None
        doc_new = Document(
            case_id=case.id,
            doc_type="resolution",
            status="stored",
            movement_id=mv_new.id,
        )
        db.add_all([doc_mid, doc_old, doc_static, doc_new])
        db.commit()
        for d in (doc_mid, doc_old, doc_static, doc_new):
            db.refresh(d)

        resp = authed_client.get(f"/api/v1/cases/{case.id}/documents")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 4

        # Exact expected doc_date sequence: newest → oldest → null last.
        dates = [it["doc_date"] for it in items]
        assert dates == ["2026-06-30", "2026-03-15", "2026-01-01", None]

        # Dated docs must be strictly non-increasing (ignoring the null tail).
        dated = [d for d in dates if d is not None]
        assert dated == sorted(dated, reverse=True)

        # Null-date (case-level) doc(s) appear LAST.
        assert dates[-1] is None
        null_positions = [i for i, d in enumerate(dates) if d is None]
        assert null_positions == list(range(len(dates) - len(null_positions), len(dates)))

        # The newest-dated doc is FIRST; the oldest-dated is LAST AMONG DATED.
        ids = [it["id"] for it in items]
        assert ids[0] == doc_new.id
        last_dated_pos = max(
            i for i, d in enumerate(dates) if d is not None
        )
        assert ids[last_dated_pos] == doc_old.id
        # And the null-date doc is the final element overall.
        assert ids[-1] == doc_static.id


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/documents/{document_id}/download
# ─────────────────────────────────────────────────────────────────────────────

class TestDownloadDocument:
    def test_returns_404_for_unknown_document(self, authed_client):
        resp = authed_client.get("/api/v1/documents/99999/download")
        assert resp.status_code == 404

    def test_returns_404_for_document_owned_by_other_lawyer(
        self, client, db, document
    ):
        """A lawyer cannot download another lawyer's document."""
        other = Lawyer(rut="44444444-4", name="Stranger Lawyer")
        db.add(other)
        db.commit()
        db.refresh(other)

        async def _stranger():
            return {"sub": str(other.id)}

        app.dependency_overrides[get_current_lawyer] = _stranger
        try:
            resp = client.get(f"/api/v1/documents/{document.id}/download")
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)
