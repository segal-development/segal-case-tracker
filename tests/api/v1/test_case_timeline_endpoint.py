"""Tests for GET /api/v1/cases/{case_id}/timeline (req #8, slice 1).

Read-only aggregation across Movement, Document, CaseDeadline, Alert,
CaseEscrito, CaseNotificacion, CaseExhorto. Same auth + case-visibility
pattern as the existing /cases/{id}/movements and /cases/{id}/documents
endpoints.
"""

from datetime import datetime

import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.models.movement import Movement


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="21212121-2", name="Timeline Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T-TL", name="Juzgado Timeline", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def case(db, lawyer, court):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-TL-2026",
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
def authed_client(client, lawyer):
    async def _mock_get_current_lawyer():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock_get_current_lawyer
    yield client
    # dependency_overrides cleared by the parent `client` fixture


class TestCaseTimelineEndpoint:
    def test_empty_case_returns_empty_list(self, authed_client, case):
        resp = authed_client.get(f"/api/v1/cases/{case.id}/timeline")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["pages"] == 0

    def test_merges_sources_sorted_most_recent_first(self, authed_client, db, case):
        movement = Movement(
            case_id=case.id,
            description="Movimiento inicial",
            movement_date=datetime(2026, 5, 1, 0, 0, 0),
        )
        db.add(movement)
        db.flush()  # need movement.id to link the document below
        # Linked to the movement → inherits its date (05-01), NOT stored_at (05-05).
        document = Document(
            case_id=case.id,
            movement_id=movement.id,
            doc_type="resolution",
            status="stored",
            downloaded_at=datetime(2026, 5, 2, 0, 0, 0),
            stored_at=datetime(2026, 5, 5, 0, 0, 0),
        )
        deadline = CaseDeadline(
            case_id=case.id,
            deadline_type="excepciones_8d",
            due_date=datetime(2026, 6, 10).date(),
            triggered_at=datetime(2026, 5, 3).date(),
            status="active",
            created_at=datetime(2026, 5, 3, 0, 0, 0),
        )
        db.add_all([document, deadline])
        db.commit()

        resp = authed_client.get(f"/api/v1/cases/{case.id}/timeline")
        assert resp.status_code == 200
        body = resp.json()
        # Alerts are no longer part of the timeline (they duplicate movements).
        assert body["total"] == 3
        kinds = [item["kind"] for item in body["items"]]
        assert "alerta" not in kinds
        # Deadline (05-03) is most recent; the document inherits its movement's
        # date (05-01), not stored_at (05-05).
        assert kinds[0] == "plazo"
        doc = next(i for i in body["items"] if i["kind"] == "documento")
        assert doc["date"].startswith("2026-05-01")

    def test_document_with_no_stored_at_falls_back_to_downloaded_at(
        self, authed_client, db, case
    ):
        document = Document(
            case_id=case.id,
            doc_type="cert_envio",
            status="pending",
            downloaded_at=datetime(2026, 4, 20, 0, 0, 0),
        )
        db.add(document)
        db.commit()

        resp = authed_client.get(f"/api/v1/cases/{case.id}/timeline")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["date"].startswith("2026-04-20")

    def test_pagination_total_and_pages(self, authed_client, db, case):
        for i in range(1, 6):
            db.add(
                Movement(
                    case_id=case.id,
                    description=f"Movimiento {i}",
                    movement_date=datetime(2026, 5, i, 0, 0, 0),
                )
            )
        db.commit()

        resp = authed_client.get(
            f"/api/v1/cases/{case.id}/timeline", params={"page": 1, "per_page": 2}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 5
        assert body["pages"] == 3
        assert len(body["items"]) == 2
        assert body["items"][0]["date"].startswith("2026-05-05")

    def test_returns_404_for_unknown_case(self, authed_client):
        resp = authed_client.get("/api/v1/cases/99999/timeline")
        assert resp.status_code == 404

    def test_returns_404_for_case_owned_by_other_lawyer(self, client, db, case):
        """Same case-visibility scoping as GET /cases/{id}/movements."""
        other = Lawyer(rut="23232323-2", name="Other Timeline Lawyer")
        db.add(other)
        db.commit()
        db.refresh(other)

        async def _other_lawyer():
            return {"sub": str(other.id)}

        app.dependency_overrides[get_current_lawyer] = _other_lawyer
        try:
            resp = client.get(f"/api/v1/cases/{case.id}/timeline")
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.pop(get_current_lawyer, None)
