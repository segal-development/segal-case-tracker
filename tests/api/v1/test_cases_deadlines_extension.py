"""Tests for GET /cases deadline extension — Strict TDD T5.3 (RED phase).

PR2 — extends CaseResponse with procedural_state / semaforo / next_deadline_at
and adds sort_by=criticidad ordering (next_deadline_at ASC NULLS LAST).
"""

import pytest
from datetime import date, datetime, timedelta
from fastapi.testclient import TestClient

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Test Lawyer Cases")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-CS", name="Juzgado Cases Sort", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_case(db, lawyer, court, rol, semaforo=None, procedural_state=None, next_deadline_at=None, updated_at=None):
    """Helper to create a Case quickly."""
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        plaintiff="BANCO TEST",
        defendant="DEUDOR TEST",
        procedural_state=procedural_state,
        semaforo=semaforo,
        next_deadline_at=next_deadline_at,
        created_at=updated_at or datetime.utcnow(),
        updated_at=updated_at or datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def single_case(db, lawyer, court):
    return _make_case(
        db, lawyer, court,
        rol="C-6001-2025",
        procedural_state="notificado",
        semaforo="amarillo",
        next_deadline_at=date.today() + timedelta(days=4),
    )


@pytest.fixture
def three_cases_for_sort(db, lawyer, court):
    """Three cases with different next_deadline_at values (incl. NULL) for sort tests."""
    # Earliest deadline — should appear first on criticidad sort
    case_urgent = _make_case(
        db, lawyer, court,
        rol="C-6010-2025",
        procedural_state="notificado",
        semaforo="rojo",
        next_deadline_at=date.today() + timedelta(days=1),
        updated_at=datetime(2025, 1, 3),  # oldest updated_at
    )
    # Upcoming deadline — second
    case_soon = _make_case(
        db, lawyer, court,
        rol="C-6011-2025",
        procedural_state="notificado",
        semaforo="amarillo",
        next_deadline_at=date.today() + timedelta(days=5),
        updated_at=datetime(2025, 1, 2),
    )
    # No deadline — should be last (NULLS LAST)
    case_null = _make_case(
        db, lawyer, court,
        rol="C-6012-2025",
        procedural_state="indeterminate",
        semaforo="gris",
        next_deadline_at=None,
        updated_at=datetime(2025, 1, 1),  # youngest updated_at
    )
    return case_urgent, case_soon, case_null


@pytest.fixture
def authed_client(client, lawyer):
    """TestClient with get_current_lawyer stubbed to the primary test lawyer."""

    async def _mock_get_current_lawyer():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock_get_current_lawyer
    yield client


# ---------------------------------------------------------------------------
# Tests: CaseResponse includes semaforo fields (REQ-8)
# ---------------------------------------------------------------------------


class TestCaseListIncludesDeadlineFields:
    """GET /cases items must contain procedural_state, semaforo, next_deadline_at."""

    def test_response_item_has_procedural_state(
        self, authed_client: TestClient, single_case: Case
    ):
        response = authed_client.get("/api/v1/cases")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) >= 1
        item = data["items"][0]
        assert "procedural_state" in item

    def test_response_item_has_semaforo(
        self, authed_client: TestClient, single_case: Case
    ):
        response = authed_client.get("/api/v1/cases")
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert "semaforo" in item

    def test_response_item_has_next_deadline_at(
        self, authed_client: TestClient, single_case: Case
    ):
        response = authed_client.get("/api/v1/cases")
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert "next_deadline_at" in item

    def test_semaforo_value_matches_case(
        self, authed_client: TestClient, single_case: Case
    ):
        response = authed_client.get("/api/v1/cases")
        item = response.json()["items"][0]
        assert item["semaforo"] == "amarillo"

    def test_procedural_state_value_matches_case(
        self, authed_client: TestClient, single_case: Case
    ):
        response = authed_client.get("/api/v1/cases")
        item = response.json()["items"][0]
        assert item["procedural_state"] == "notificado"

    def test_next_deadline_at_value_matches_case(
        self, authed_client: TestClient, single_case: Case
    ):
        expected = (date.today() + timedelta(days=4)).isoformat()
        response = authed_client.get("/api/v1/cases")
        item = response.json()["items"][0]
        assert item["next_deadline_at"] == expected

    def test_null_semaforo_case_shows_none(
        self, authed_client: TestClient, db, court: Court, lawyer: Lawyer
    ):
        """A case without semaforo set should return null in response."""
        case = _make_case(db, lawyer, court, rol="C-6099-2025")
        response = authed_client.get("/api/v1/cases")
        items = response.json()["items"]
        matching = [i for i in items if i["id"] == case.id]
        assert len(matching) == 1
        assert matching[0]["semaforo"] is None
        assert matching[0]["procedural_state"] is None
        assert matching[0]["next_deadline_at"] is None


# ---------------------------------------------------------------------------
# Tests: sort_by=criticidad (REQ-8 scenario)
# ---------------------------------------------------------------------------


class TestCaseSortByCriticidad:
    """sort_by=criticidad orders by next_deadline_at ASC NULLS LAST."""

    def test_sort_by_criticidad_most_urgent_first(
        self, authed_client: TestClient, three_cases_for_sort
    ):
        """The case with the earliest (smallest) next_deadline_at comes first."""
        case_urgent, case_soon, case_null = three_cases_for_sort

        response = authed_client.get("/api/v1/cases?sort_by=criticidad")
        assert response.status_code == 200
        items = response.json()["items"]

        # Filter to only our 3 test cases by rol
        rols = [i["rol"] for i in items]
        urgent_idx = rols.index("C-6010-2025")
        soon_idx = rols.index("C-6011-2025")
        null_idx = rols.index("C-6012-2025")

        assert urgent_idx < soon_idx, "Urgent case must precede soon case"
        assert soon_idx < null_idx, "Soon case must precede null-deadline case"

    def test_sort_by_criticidad_null_deadline_last(
        self, authed_client: TestClient, three_cases_for_sort
    ):
        """Cases with NULL next_deadline_at come last (NULLS LAST)."""
        case_urgent, case_soon, case_null = three_cases_for_sort

        response = authed_client.get("/api/v1/cases?sort_by=criticidad")
        items = response.json()["items"]

        # The last item among our 3 should be the null-deadline case
        our_items = [i for i in items if i["rol"] in ("C-6010-2025", "C-6011-2025", "C-6012-2025")]
        assert len(our_items) == 3
        assert our_items[-1]["rol"] == "C-6012-2025"
        assert our_items[-1]["next_deadline_at"] is None

    def test_sort_by_criticidad_returns_200(
        self, authed_client: TestClient, single_case: Case
    ):
        response = authed_client.get("/api/v1/cases?sort_by=criticidad")
        assert response.status_code == 200

    def test_default_sort_still_updated_at_desc(
        self, authed_client: TestClient, three_cases_for_sort
    ):
        """Default sort (no sort_by) must still be updated_at DESC."""
        case_urgent, case_soon, case_null = three_cases_for_sort

        # Without sort_by, order is updated_at DESC:
        # case_urgent updated_at=2025-01-03 → first
        # case_soon   updated_at=2025-01-02 → second
        # case_null   updated_at=2025-01-01 → third
        response = authed_client.get("/api/v1/cases")
        assert response.status_code == 200
        items = response.json()["items"]

        our_items = [i for i in items if i["rol"] in ("C-6010-2025", "C-6011-2025", "C-6012-2025")]
        assert len(our_items) == 3
        rols = [i["rol"] for i in our_items]
        assert rols == ["C-6010-2025", "C-6011-2025", "C-6012-2025"], (
            f"Expected updated_at DESC order, got: {rols}"
        )


# ---------------------------------------------------------------------------
# Tests: auth + scoping regression
# ---------------------------------------------------------------------------


class TestCasesListAuthRegression:
    """Existing auth and scoping must still work after the extension."""

    def test_auth_required(self, client: TestClient, single_case: Case):
        """No bearer token → 401."""
        response = client.get("/api/v1/cases")
        assert response.status_code == 401

    def test_only_own_cases_returned(
        self, authed_client: TestClient, db, court: Court, lawyer: Lawyer, single_case: Case
    ):
        """Cases from other lawyers must not appear in the list."""
        other = Lawyer(rut="33333333-3", name="Otro Abogado Cases")
        db.add(other)
        db.commit()
        db.refresh(other)

        other_case = _make_case(db, other, court, rol="C-6999-2025")

        response = authed_client.get("/api/v1/cases")
        items = response.json()["items"]
        returned_ids = {i["id"] for i in items}

        assert single_case.id in returned_ids
        assert other_case.id not in returned_ids

    def test_pagination_still_works(
        self, authed_client: TestClient, three_cases_for_sort
    ):
        """per_page limit must still work after extension."""
        response = authed_client.get("/api/v1/cases?per_page=2")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2
        assert data["total"] == 3
