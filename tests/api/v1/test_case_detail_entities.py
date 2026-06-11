"""Tests for GET /{case_id} entity fields and GET /{case_id}/detail-entities endpoint.

Strict TDD — written before implementation. All tests should be RED until
app/api/v1/cases.py exposes the 4 entity lists.
"""

import pytest
from datetime import datetime
from fastapi.testclient import TestClient

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.case_litigante import CaseLitigante
from app.models.case_notificacion import CaseNotificacion
from app.models.court import Court
from app.models.lawyer import Lawyer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Test Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-ENT", name="Juzgado Entidades", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def case(db, lawyer, court):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-1001-2025",
        status="active",
        competencia="civil",
        plaintiff="BANCO TEST",
        defendant="FERNANDEZ",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def authed_client(client, lawyer):
    """TestClient with get_current_lawyer stubbed to the primary test lawyer."""

    async def _mock_get_current_lawyer():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock_get_current_lawyer
    yield client
    # dependency_overrides cleared by the parent `client` fixture teardown


@pytest.fixture
def seeded_entities(db, case):
    """Seed one row of each entity type linked to the case."""
    litigante = CaseLitigante(
        case_id=case.id,
        participante="DTE.",
        rut="81826800-9",
        persona_type="JURIDICA",
        nombre="BANCO TEST S.A.",
        natural_key="81826800-9|dte.",
    )
    notificacion = CaseNotificacion(
        case_id=case.id,
        rol="C-1001-2025",
        estado_notif="REALIZADA",
        tipo_notif="PERSONAL",
        tipo_participante="DDO.",
        nombre="FERNANDEZ",
        tramite="Demanda",
        obs_fallida=None,
        natural_key="notif-001",
    )
    escrito = CaseEscrito(
        case_id=case.id,
        tipo_escrito="DEMANDA",
        solicitante="BANCO TEST",
        tiene_documento=True,
        tiene_anexo=False,
        doc_token=None,
        natural_key="escrito-001",
    )
    exhorto = CaseExhorto(
        case_id=case.id,
        rol_origen="C-1001-2025",
        tipo_exhorto="ACTIVO",
        rol_destino="E-355-2026",
        tribunal_destino="JUZGADO DE LETRAS TEST",
        estado="PENDIENTE",
        natural_key="exhorto-001",
    )
    db.add_all([litigante, notificacion, escrito, exhorto])
    db.commit()
    return {
        "litigante": litigante,
        "notificacion": notificacion,
        "escrito": escrito,
        "exhorto": exhorto,
    }


# ---------------------------------------------------------------------------
# GET /{case_id} — entity fields embedded in the existing detail response
# ---------------------------------------------------------------------------


class TestGetCaseIncludesEntities:
    """GET /{case_id} must include all 4 entity lists in the response."""

    def test_entities_present_when_seeded(
        self, authed_client: TestClient, case: Case, seeded_entities: dict
    ):
        response = authed_client.get(f"/api/v1/cases/{case.id}")
        assert response.status_code == 200
        data = response.json()

        # Litigantes
        assert len(data["litigantes"]) == 1
        lit = data["litigantes"][0]
        assert lit["rut"] == "81826800-9"
        assert lit["participante"] == "DTE."
        assert lit["persona_type"] == "JURIDICA"
        assert lit["nombre"] == "BANCO TEST S.A."

        # Notificaciones
        assert len(data["notificaciones"]) == 1
        notif = data["notificaciones"][0]
        assert notif["rol"] == "C-1001-2025"
        assert notif["estado_notif"] == "REALIZADA"
        assert notif["tipo_notif"] == "PERSONAL"
        assert notif["nombre"] == "FERNANDEZ"

        # Escritos
        assert len(data["escritos"]) == 1
        escrito = data["escritos"][0]
        assert escrito["tipo_escrito"] == "DEMANDA"
        assert escrito["solicitante"] == "BANCO TEST"
        assert escrito["tiene_documento"] is True
        assert escrito["tiene_anexo"] is False

        # Exhortos
        assert len(data["exhortos"]) == 1
        exhorto = data["exhortos"][0]
        assert exhorto["rol_origen"] == "C-1001-2025"
        assert exhorto["tipo_exhorto"] == "ACTIVO"
        assert exhorto["rol_destino"] == "E-355-2026"
        assert exhorto["tribunal_destino"] == "JUZGADO DE LETRAS TEST"
        assert exhorto["estado"] == "PENDIENTE"

    def test_count_fields_match_list_lengths(
        self, authed_client: TestClient, case: Case, seeded_entities: dict
    ):
        response = authed_client.get(f"/api/v1/cases/{case.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["litigantes_count"] == len(data["litigantes"])
        assert data["notificaciones_count"] == len(data["notificaciones"])
        assert data["escritos_count"] == len(data["escritos"])
        assert data["exhortos_count"] == len(data["exhortos"])

    def test_empty_case_returns_empty_entity_lists(
        self, authed_client: TestClient, case: Case
    ):
        """A case with no entity rows returns empty lists — no error."""
        response = authed_client.get(f"/api/v1/cases/{case.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["litigantes"] == []
        assert data["notificaciones"] == []
        assert data["escritos"] == []
        assert data["exhortos"] == []
        assert data["litigantes_count"] == 0
        assert data["notificaciones_count"] == 0
        assert data["escritos_count"] == 0
        assert data["exhortos_count"] == 0

    def test_movements_still_returned(
        self, authed_client: TestClient, case: Case, seeded_entities: dict
    ):
        """Existing movements field must still be present (no regression)."""
        response = authed_client.get(f"/api/v1/cases/{case.id}")
        assert response.status_code == 200
        data = response.json()
        assert "movements" in data
        assert "movements_count" in data

    def test_auth_required(self, client: TestClient, case: Case):
        response = client.get(f"/api/v1/cases/{case.id}")
        assert response.status_code == 401

    def test_other_lawyers_case_returns_404(
        self, authed_client: TestClient, db, court: Court
    ):
        """A case owned by a different lawyer must return 404."""
        other = Lawyer(rut="22222222-2", name="Other Lawyer")
        db.add(other)
        db.commit()
        db.refresh(other)
        other_case = Case(
            lawyer_id=other.id,
            court_id=court.id,
            rol="C-9999-2025",
            status="active",
            competencia="civil",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(other_case)
        db.commit()
        db.refresh(other_case)

        response = authed_client.get(f"/api/v1/cases/{other_case.id}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /{case_id}/detail-entities — lightweight standalone endpoint
# ---------------------------------------------------------------------------


class TestDetailEntitiesEndpoint:
    """GET /{case_id}/detail-entities returns only the 4 entity lists."""

    def test_returns_all_four_entity_lists(
        self, authed_client: TestClient, case: Case, seeded_entities: dict
    ):
        response = authed_client.get(f"/api/v1/cases/{case.id}/detail-entities")
        assert response.status_code == 200
        data = response.json()

        assert "litigantes" in data
        assert "notificaciones" in data
        assert "escritos" in data
        assert "exhortos" in data

        assert len(data["litigantes"]) == 1
        assert data["litigantes"][0]["rut"] == "81826800-9"

        assert len(data["notificaciones"]) == 1
        assert data["notificaciones"][0]["rol"] == "C-1001-2025"

        assert len(data["escritos"]) == 1
        assert data["escritos"][0]["tipo_escrito"] == "DEMANDA"

        assert len(data["exhortos"]) == 1
        assert data["exhortos"][0]["estado"] == "PENDIENTE"

    def test_no_movements_or_case_object_in_response(
        self, authed_client: TestClient, case: Case, seeded_entities: dict
    ):
        """detail-entities must NOT include movements or the full case object."""
        response = authed_client.get(f"/api/v1/cases/{case.id}/detail-entities")
        assert response.status_code == 200
        data = response.json()
        assert "movements" not in data
        assert "case" not in data

    def test_empty_entities_returns_empty_lists(
        self, authed_client: TestClient, case: Case
    ):
        response = authed_client.get(f"/api/v1/cases/{case.id}/detail-entities")
        assert response.status_code == 200
        data = response.json()
        assert data["litigantes"] == []
        assert data["notificaciones"] == []
        assert data["escritos"] == []
        assert data["exhortos"] == []

    def test_auth_required(self, client: TestClient, case: Case):
        response = client.get(f"/api/v1/cases/{case.id}/detail-entities")
        assert response.status_code == 401

    def test_unknown_case_returns_404(self, authed_client: TestClient):
        response = authed_client.get("/api/v1/cases/99999/detail-entities")
        assert response.status_code == 404
