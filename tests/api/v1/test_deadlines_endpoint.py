"""Tests for GET /cases/{case_id}/deadlines — Strict TDD T5.1 (RED phase).

PR2 — abogado-facing API over the deadline engine built in PR1.
Auth required; lawyer-scoped (404 if case not owned by caller).
DEADLINE_DISCLAIMER must appear in every response.
"""

import pytest
from datetime import date, datetime, timedelta
from fastapi.testclient import TestClient

from app.api.deps import get_current_lawyer
from app.core.deadlines_config import DEADLINE_DISCLAIMER, DeadlineType
from app.main import app
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.lawyer import Lawyer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Test Lawyer DL")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-DL", name="Juzgado Deadlines", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def civil_case(db, lawyer, court):
    """A civil case in NOTIFICADO/amarillo state (no deadline rows yet)."""
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-5001-2025",
        status="active",
        competencia="civil",
        plaintiff="BANCO TEST",
        defendant="CLIENTE TEST",
        procedural_state="notificado",
        semaforo="amarillo",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def gris_case(db, lawyer, court):
    """A case in INDETERMINATE/gris state (no active deadlines)."""
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-5002-2025",
        status="active",
        competencia="civil",
        plaintiff="BANCO GRIS",
        defendant="DEUDOR GRIS",
        procedural_state="indeterminate",
        semaforo="gris",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def case_with_active_deadline(db, civil_case):
    """Add an active EXCEPCIONES_8D deadline row to the civil case."""
    # Due date 5 calendar days from now so it stays 'amarillo'
    due = date.today() + timedelta(days=5)
    triggered = date.today() - timedelta(days=3)

    deadline = CaseDeadline(
        case_id=civil_case.id,
        deadline_type=DeadlineType.EXCEPCIONES_8D.value,
        legal_basis="art. 459 CPC",
        due_date=due,
        triggered_at=triggered,
        status="active",
        computed_at=datetime.utcnow(),
    )
    db.add(deadline)
    civil_case.next_deadline_at = due
    db.commit()
    db.refresh(civil_case)
    return civil_case


@pytest.fixture
def case_with_recommendation(db, civil_case):
    """A case with a DecisionEngine recommendation already computed."""
    civil_case.recommended_action_code = "oponer_excepciones"
    civil_case.next_review_at = date.today() + timedelta(days=5)
    db.commit()
    db.refresh(civil_case)
    return civil_case


@pytest.fixture
def case_without_recommendation(db, civil_case):
    """A case with no pending recommendation (nothing to do)."""
    civil_case.recommended_action_code = None
    civil_case.next_review_at = None
    db.commit()
    db.refresh(civil_case)
    return civil_case


@pytest.fixture
def authed_client(client, lawyer):
    """TestClient with get_current_lawyer stubbed to the primary test lawyer."""

    async def _mock_get_current_lawyer():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock_get_current_lawyer
    yield client
    # dependency_overrides cleared by the parent `client` fixture teardown


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------


class TestDeadlinesEndpointHappyPath:
    """GET /cases/{id}/deadlines returns all required response fields."""

    def test_returns_200_with_all_required_fields(
        self, authed_client: TestClient, civil_case: Case
    ):
        """Response body must contain every field from REQ-7 and design."""
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        assert response.status_code == 200
        data = response.json()

        assert "case_id" in data
        assert "procedural_state" in data
        assert "semaforo" in data
        assert "active_deadlines" in data
        assert "proxima_accion" in data
        assert "abandono_risk" in data
        assert "prescripcion_risk" in data
        assert "disclaimer" in data

    def test_case_id_matches_path(
        self, authed_client: TestClient, civil_case: Case
    ):
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        assert response.status_code == 200
        assert response.json()["case_id"] == civil_case.id

    def test_procedural_state_and_semaforo_reflect_case(
        self, authed_client: TestClient, civil_case: Case
    ):
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        data = response.json()
        assert data["procedural_state"] == "notificado"
        assert data["semaforo"] == "amarillo"


# ---------------------------------------------------------------------------
# Tests: disclaimer (REQ-10 + review flag)
# ---------------------------------------------------------------------------


class TestDeadlinesDisclaimer:
    """disclaimer field must always be present and equal the constant."""

    def test_disclaimer_equals_constant(
        self, authed_client: TestClient, civil_case: Case
    ):
        """disclaimer must be the DEADLINE_DISCLAIMER module constant (exact match)."""
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        assert response.status_code == 200
        assert response.json()["disclaimer"] == DEADLINE_DISCLAIMER

    def test_disclaimer_is_non_empty(
        self, authed_client: TestClient, civil_case: Case
    ):
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        disclaimer = response.json()["disclaimer"]
        assert disclaimer  # truthy — not None, not ""

    def test_disclaimer_contains_no_reemplaza_phrase(
        self, authed_client: TestClient, civil_case: Case
    ):
        """REQ-7 scenario: disclaimer must contain 'no reemplaza el criterio del abogado'."""
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        disclaimer = response.json()["disclaimer"]
        assert "no reemplaza el criterio del abogado" in disclaimer

    def test_disclaimer_present_on_gris_case(
        self, authed_client: TestClient, gris_case: Case
    ):
        """disclaimer must be present even when the case is INDETERMINATE/gris."""
        response = authed_client.get(f"/api/v1/cases/{gris_case.id}/deadlines")
        assert response.status_code == 200
        assert response.json()["disclaimer"] == DEADLINE_DISCLAIMER


# ---------------------------------------------------------------------------
# Tests: auth + lawyer scoping
# ---------------------------------------------------------------------------


class TestDeadlinesAuthAndScoping:
    """Authentication required; case must belong to the requesting lawyer."""

    def test_auth_required_returns_401(
        self, client: TestClient, civil_case: Case
    ):
        """No bearer token → 401."""
        response = client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        assert response.status_code == 401

    def test_other_lawyers_case_returns_404(
        self, authed_client: TestClient, db, court: Court
    ):
        """Case owned by a different lawyer must return 404 (not 403)."""
        other = Lawyer(rut="22222222-2", name="Other Lawyer DL")
        db.add(other)
        db.commit()
        db.refresh(other)

        other_case = Case(
            lawyer_id=other.id,
            court_id=court.id,
            rol="C-9999-2025",
            status="active",
            competencia="civil",
            procedural_state="notificado",
            semaforo="amarillo",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(other_case)
        db.commit()
        db.refresh(other_case)

        response = authed_client.get(f"/api/v1/cases/{other_case.id}/deadlines")
        assert response.status_code == 404

    def test_unknown_case_returns_404(
        self, authed_client: TestClient
    ):
        response = authed_client.get("/api/v1/cases/99999/deadlines")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tests: active deadlines timeline
# ---------------------------------------------------------------------------


class TestDeadlinesTimeline:
    """active_deadlines list shape and content."""

    def test_active_deadline_shows_legal_basis(
        self, authed_client: TestClient, case_with_active_deadline: Case
    ):
        """Each active deadline must expose legal_basis (CPC article)."""
        response = authed_client.get(
            f"/api/v1/cases/{case_with_active_deadline.id}/deadlines"
        )
        assert response.status_code == 200
        data = response.json()

        assert len(data["active_deadlines"]) >= 1
        dl = data["active_deadlines"][0]
        assert dl["legal_basis"] == "art. 459 CPC"

    def test_active_deadline_has_dias_habiles_remaining(
        self, authed_client: TestClient, case_with_active_deadline: Case
    ):
        """Each deadline item must include dias_habiles_remaining."""
        response = authed_client.get(
            f"/api/v1/cases/{case_with_active_deadline.id}/deadlines"
        )
        data = response.json()
        dl = data["active_deadlines"][0]
        assert "dias_habiles_remaining" in dl
        assert isinstance(dl["dias_habiles_remaining"], int)

    def test_active_deadline_has_source_movement_reference(
        self, authed_client: TestClient, case_with_active_deadline: Case
    ):
        """Each deadline item must expose source_movement_id (may be null)."""
        response = authed_client.get(
            f"/api/v1/cases/{case_with_active_deadline.id}/deadlines"
        )
        data = response.json()
        dl = data["active_deadlines"][0]
        # source_movement_id key must be present (value may be null)
        assert "source_movement_id" in dl

    def test_active_deadline_has_deadline_type(
        self, authed_client: TestClient, case_with_active_deadline: Case
    ):
        response = authed_client.get(
            f"/api/v1/cases/{case_with_active_deadline.id}/deadlines"
        )
        data = response.json()
        dl = data["active_deadlines"][0]
        assert dl["deadline_type"] == DeadlineType.EXCEPCIONES_8D.value

    def test_gris_case_returns_empty_active_deadlines(
        self, authed_client: TestClient, gris_case: Case
    ):
        """INDETERMINATE/gris case → empty active_deadlines list, semaforo == 'gris'."""
        response = authed_client.get(f"/api/v1/cases/{gris_case.id}/deadlines")
        assert response.status_code == 200
        data = response.json()
        assert data["active_deadlines"] == []
        assert data["semaforo"] == "gris"


# ---------------------------------------------------------------------------
# Tests: próxima acción
# ---------------------------------------------------------------------------


class TestProximaAccion:
    """proxima_accion is the nearest active deadline with a human description."""

    def test_proxima_accion_present_when_active_deadline_exists(
        self, authed_client: TestClient, case_with_active_deadline: Case
    ):
        """proxima_accion must be a non-null object when active deadlines exist."""
        response = authed_client.get(
            f"/api/v1/cases/{case_with_active_deadline.id}/deadlines"
        )
        data = response.json()
        assert data["proxima_accion"] is not None

    def test_proxima_accion_has_required_fields(
        self, authed_client: TestClient, case_with_active_deadline: Case
    ):
        """proxima_accion must contain deadline_type, due_date, dias_habiles_remaining, description."""
        response = authed_client.get(
            f"/api/v1/cases/{case_with_active_deadline.id}/deadlines"
        )
        pa = response.json()["proxima_accion"]
        assert "deadline_type" in pa
        assert "due_date" in pa
        assert "dias_habiles_remaining" in pa
        assert "description" in pa

    def test_proxima_accion_none_when_no_active_deadlines(
        self, authed_client: TestClient, gris_case: Case
    ):
        """proxima_accion must be null when there are no active deadlines."""
        response = authed_client.get(f"/api/v1/cases/{gris_case.id}/deadlines")
        data = response.json()
        assert data["proxima_accion"] is None


# ---------------------------------------------------------------------------
# Tests: abandono_risk and prescripcion_risk flags
# ---------------------------------------------------------------------------


class TestAbandonoPrescripcionFlags:
    """abandono_risk and prescripcion_risk are always present (may be 'none')."""

    def test_abandono_risk_present_and_string(
        self, authed_client: TestClient, civil_case: Case
    ):
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        data = response.json()
        assert "abandono_risk" in data
        assert isinstance(data["abandono_risk"], str)

    def test_prescripcion_risk_present_and_string(
        self, authed_client: TestClient, civil_case: Case
    ):
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        data = response.json()
        assert "prescripcion_risk" in data
        assert isinstance(data["prescripcion_risk"], str)

    def test_abandono_risk_none_for_recent_case(
        self, authed_client: TestClient, civil_case: Case
    ):
        """Case with last_movement_at = today → abandono_risk == 'none'."""
        # civil_case has no last_movement_at set → should return "none"
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        data = response.json()
        assert data["abandono_risk"] == "none"

    def test_prescripcion_risk_none_for_recent_case(
        self, authed_client: TestClient, civil_case: Case
    ):
        """Case with no filed_at → prescripcion_risk == 'none'."""
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        data = response.json()
        assert data["prescripcion_risk"] == "none"


# ---------------------------------------------------------------------------
# Tests: recommended_action + next_review_at (DecisionEngine, req #5/#11)
# ---------------------------------------------------------------------------


class TestRecommendedAction:
    """GET .../deadlines resolves Case.recommended_action_code into a real
    recommendation object (action_text/legal_basis/urgency/disclaimer)."""

    def test_response_contains_recommended_action_and_next_review_at_keys(
        self, authed_client: TestClient, civil_case: Case
    ):
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        assert response.status_code == 200
        data = response.json()
        assert "recommended_action" in data
        assert "next_review_at" in data

    def test_recommended_action_resolved_from_code(
        self, authed_client: TestClient, case_with_recommendation: Case
    ):
        response = authed_client.get(f"/api/v1/cases/{case_with_recommendation.id}/deadlines")
        data = response.json()
        rec = data["recommended_action"]
        assert rec is not None
        assert rec["code"] == "oponer_excepciones"
        assert rec["action_text"] == "Oponer excepciones (escrito de oposición)"
        assert rec["legal_basis"] == "art. 459/464 CPC"
        assert rec["urgency"] == "critica"

    def test_recommended_action_carries_disclaimer(
        self, authed_client: TestClient, case_with_recommendation: Case
    ):
        response = authed_client.get(f"/api/v1/cases/{case_with_recommendation.id}/deadlines")
        rec = response.json()["recommended_action"]
        assert rec["disclaimer"]
        assert "abogado" in rec["disclaimer"].lower()

    def test_next_review_at_reflects_case_column(
        self, authed_client: TestClient, case_with_recommendation: Case
    ):
        response = authed_client.get(f"/api/v1/cases/{case_with_recommendation.id}/deadlines")
        data = response.json()
        assert data["next_review_at"] == case_with_recommendation.next_review_at.isoformat()

    def test_recommended_action_none_when_code_is_none(
        self, authed_client: TestClient, case_without_recommendation: Case
    ):
        response = authed_client.get(f"/api/v1/cases/{case_without_recommendation.id}/deadlines")
        data = response.json()
        assert data["recommended_action"] is None
        assert data["next_review_at"] is None

    def test_recommended_action_none_when_code_unknown(
        self, authed_client: TestClient, db, civil_case: Case
    ):
        """A stale/unrecognised code (e.g. after a rule was renamed) must
        resolve to None rather than 500ing the endpoint."""
        civil_case.recommended_action_code = "totally_unknown_code"
        db.commit()
        response = authed_client.get(f"/api/v1/cases/{civil_case.id}/deadlines")
        assert response.status_code == 200
        assert response.json()["recommended_action"] is None
