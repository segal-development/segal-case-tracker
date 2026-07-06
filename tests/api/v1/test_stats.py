"""Tests for firm_dashboard_stats helper and GET /api/v1/stats/firm endpoint."""

import pytest
from datetime import datetime, timedelta

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.document import Document
from app.models.movement import Movement
from app.services.lawyer_roster import firm_dashboard_stats, admin_dashboard_stats


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACCOUNT_RUT = "11111111-1"
FIRM_LAWYER_RUT = "22222222-2"
OPPOSING_RUT = "33333333-3"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=ACCOUNT_RUT, name="Account Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-ST", name="Juzgado Stats", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_case(db, lawyer, court, rol, semaforo=None, last_movement_at=None,
               matter=None, procedure=None, procedural_state=None):
    """Create and return a civil Case owned by lawyer."""
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        semaforo=semaforo,
        last_movement_at=last_movement_at,
        matter=matter,
        procedure=procedure,
        procedural_state=procedural_state,
        plaintiff="BANCO DEMANDANTE",
        defendant="DEUDOR DDO",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _seed_litigantes(db, case, account_rut=ACCOUNT_RUT, firm_rut=FIRM_LAWYER_RUT,
                     firm_nombre="Firm Lawyer", opposing_rut=OPPOSING_RUT):
    """Seed account + firm lawyer as AB.DDO, opposing as AB.DTE."""
    suffix = case.rol.replace("-", "")
    lits = [
        CaseLitigante(
            case_id=case.id,
            participante="AB.DDO",
            rut=account_rut,
            persona_type="NATURAL",
            nombre="Account Lawyer",
            natural_key=f"{case.id}-acct-{suffix}",
        ),
        CaseLitigante(
            case_id=case.id,
            participante="AB.DDO",
            rut=firm_rut,
            persona_type="NATURAL",
            nombre=firm_nombre,
            natural_key=f"{case.id}-firm-{suffix}",
        ),
        CaseLitigante(
            case_id=case.id,
            participante="AB.DTE",
            rut=opposing_rut,
            persona_type="NATURAL",
            nombre="Opposing Lawyer",
            natural_key=f"{case.id}-opp-{suffix}",
        ),
    ]
    db.add_all(lits)
    db.commit()


# ---------------------------------------------------------------------------
# Unit tests: firm_dashboard_stats
# ---------------------------------------------------------------------------


class TestFirmDashboardStats:
    def test_totals_cases_count(self, db, lawyer, court):
        c1 = _make_case(db, lawyer, court, "C-5001-2025")
        c2 = _make_case(db, lawyer, court, "C-5002-2025")
        _seed_litigantes(db, c1)
        _seed_litigantes(db, c2)
        result = firm_dashboard_stats(db, ACCOUNT_RUT)
        assert result["totals"]["cases"] == 2

    def test_semaforo_bucketing_null_to_otros(self, db, lawyer, court):
        c1 = _make_case(db, lawyer, court, "C-5003-2025", semaforo=None)
        c2 = _make_case(db, lawyer, court, "C-5004-2025", semaforo="gris")
        c3 = _make_case(db, lawyer, court, "C-5005-2025", semaforo="rojo")
        _seed_litigantes(db, c1)
        _seed_litigantes(db, c2)
        _seed_litigantes(db, c3)
        result = firm_dashboard_stats(db, ACCOUNT_RUT)
        sem = result["totals"]["semaforo"]
        assert sem["otros"] == 2
        assert sem["rojo"] == 1
        assert sem["amarillo"] == 0
        assert sem["verde"] == 0

    def test_stale_logic(self, db, lawyer, court):
        fresh_dt = datetime.utcnow() - timedelta(days=10)
        old_dt = datetime.utcnow() - timedelta(days=40)
        c1 = _make_case(db, lawyer, court, "C-5006-2025", last_movement_at=fresh_dt)
        c2 = _make_case(db, lawyer, court, "C-5007-2025", last_movement_at=old_dt)
        c3 = _make_case(db, lawyer, court, "C-5008-2025", last_movement_at=None)
        _seed_litigantes(db, c1)
        _seed_litigantes(db, c2)
        _seed_litigantes(db, c3)
        result = firm_dashboard_stats(db, ACCOUNT_RUT)
        assert result["totals"]["stale"] == 2

    def test_by_lawyer_rojo_count(self, db, lawyer, court):
        c1 = _make_case(db, lawyer, court, "C-5009-2025", semaforo="rojo")
        c2 = _make_case(db, lawyer, court, "C-5010-2025", semaforo="rojo")
        _seed_litigantes(db, c1)
        _seed_litigantes(db, c2)
        result = firm_dashboard_stats(db, ACCOUNT_RUT)
        firm_entry = next(
            (e for e in result["by_lawyer"] if e["rut"] == FIRM_LAWYER_RUT), None
        )
        assert firm_entry is not None
        assert firm_entry["rojo"] == 2

    def test_by_materia_top12(self, db, lawyer, court):
        for i in range(13):
            c = _make_case(db, lawyer, court, f"C-52{i:02d}-2025", procedure=f"Materia {i}")
            _seed_litigantes(db, c)
        result = firm_dashboard_stats(db, ACCOUNT_RUT)
        assert len(result["totals"]["by_materia"]) == 12

    def test_none_materia_becomes_sin_materia(self, db, lawyer, court):
        c = _make_case(db, lawyer, court, "C-5301-2025", procedure=None)
        _seed_litigantes(db, c)
        result = firm_dashboard_stats(db, ACCOUNT_RUT)
        materias = [m["materia"] for m in result["totals"]["by_materia"]]
        assert "Sin materia" in materias

    def test_empty_for_unknown_account(self, db):
        result = firm_dashboard_stats(db, "99999999-9")
        assert result["totals"]["cases"] == 0
        assert result["by_lawyer"] == []

    def test_totals_include_cases_synced_under_different_lawyer_id(self, db, lawyer, court):
        """Firm-wide: a civil case synced under a different lawyer's account
        (Case.lawyer_id != the querying account's own id) still counts toward
        firm-wide totals when the account is an abogado-of-record litigante."""
        other_lawyer = Lawyer(rut="77777777-7", name="Other Syncer")
        db.add(other_lawyer)
        db.commit()
        db.refresh(other_lawyer)

        c1 = _make_case(db, lawyer, court, "C-5010-2025")
        c2 = _make_case(db, other_lawyer, court, "C-5011-2025")
        _seed_litigantes(db, c1)
        _seed_litigantes(db, c2)

        result = firm_dashboard_stats(db, ACCOUNT_RUT)
        assert result["totals"]["cases"] == 2

    def test_by_procedural_state_counts_and_order(self, db, lawyer, court):
        c1 = _make_case(db, lawyer, court, "C-7001-2025", procedural_state="notificado")
        c2 = _make_case(db, lawyer, court, "C-7002-2025", procedural_state="notificado")
        c3 = _make_case(db, lawyer, court, "C-7003-2025", procedural_state="excepciones")
        c4 = _make_case(db, lawyer, court, "C-7004-2025", procedural_state="sentencia")
        c5 = _make_case(db, lawyer, court, "C-7005-2025", procedural_state=None)
        for c in (c1, c2, c3, c4, c5):
            _seed_litigantes(db, c)
        result = firm_dashboard_stats(db, ACCOUNT_RUT)
        stages = result["totals"]["by_procedural_state"]
        assert isinstance(stages, list)
        stage_map = {s["stage"]: s["count"] for s in stages}
        assert stage_map["notificado"] == 2
        assert stage_map["excepciones"] == 1
        assert stage_map["sentencia"] == 1
        assert stage_map["sin_clasificar"] == 1
        stage_names = [s["stage"] for s in stages]
        assert stage_names.index("notificado") < stage_names.index("excepciones")
        assert stage_names.index("excepciones") < stage_names.index("sentencia")
        assert stage_names.index("sentencia") < stage_names.index("sin_clasificar")

    def test_by_procedural_state_omits_empty_stages(self, db, lawyer, court):
        c = _make_case(db, lawyer, court, "C-7006-2025", procedural_state="mandamiento")
        _seed_litigantes(db, c)
        result = firm_dashboard_stats(db, ACCOUNT_RUT)
        stages = result["totals"]["by_procedural_state"]
        assert len(stages) == 1
        assert stages[0] == {"stage": "mandamiento", "count": 1}

    def test_by_lawyer_sorted_by_case_count_desc(self, db, lawyer, court):
        """Lawyer on 2 cases ranks above lawyer on 1 case."""
        c1 = _make_case(db, lawyer, court, "C-5401-2025")
        c2 = _make_case(db, lawyer, court, "C-5402-2025")
        c3 = _make_case(db, lawyer, court, "C-5403-2025")
        _seed_litigantes(db, c1)
        _seed_litigantes(db, c2)
        _seed_litigantes(db, c3)
        # Add a second firm lawyer only to c3
        OTHER_RUT = "44444444-4"
        db.add(CaseLitigante(
            case_id=c3.id,
            participante="AB.DDO",
            rut=OTHER_RUT,
            persona_type="NATURAL",
            nombre="Other Lawyer",
            natural_key=f"{c3.id}-other",
        ))
        db.commit()
        result = firm_dashboard_stats(db, ACCOUNT_RUT)
        ruts = [e["rut"] for e in result["by_lawyer"]]
        assert ruts.index(FIRM_LAWYER_RUT) < ruts.index(OTHER_RUT)


# ---------------------------------------------------------------------------
# Endpoint fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def authed_client(client, lawyer):
    async def _mock():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock
    yield client


# ---------------------------------------------------------------------------
# Endpoint tests: GET /api/v1/stats/firm
# ---------------------------------------------------------------------------


class TestFirmStatsEndpoint:
    def test_endpoint_returns_200_correct_shape(self, authed_client, db, lawyer, court):
        c = _make_case(db, lawyer, court, "C-5501-2025", semaforo="verde")
        _seed_litigantes(db, c)
        response = authed_client.get("/api/v1/stats/firm")
        assert response.status_code == 200
        data = response.json()
        assert "totals" in data
        assert "by_lawyer" in data
        totals = data["totals"]
        assert "cases" in totals
        assert "semaforo" in totals
        assert "stale" in totals
        assert "by_materia" in totals
        assert totals["cases"] == 1
        sem = totals["semaforo"]
        assert set(sem.keys()) >= {"rojo", "amarillo", "verde", "otros"}

    def test_endpoint_returns_401_without_auth(self, client):
        response = client.get("/api/v1/stats/firm")
        assert response.status_code == 401

    def test_firm_endpoint_by_procedural_state_in_response(self, authed_client, db, lawyer, court):
        c = _make_case(db, lawyer, court, "C-7101-2025", procedural_state="notificado")
        _seed_litigantes(db, c)
        response = authed_client.get("/api/v1/stats/firm")
        assert response.status_code == 200
        data = response.json()
        stages = data["totals"]["by_procedural_state"]
        assert isinstance(stages, list)
        assert len(stages) >= 1
        assert "stage" in stages[0]
        assert "count" in stages[0]


# ---------------------------------------------------------------------------
# Unit tests: admin_dashboard_stats
# ---------------------------------------------------------------------------


class TestAdminDashboardStats:
    def test_pending_detail_vs_checked(self, db, lawyer, court):
        checked = _make_case(db, lawyer, court, "C-6001-2025")
        unchecked = _make_case(db, lawyer, court, "C-6002-2025")
        _seed_litigantes(db, checked)
        _seed_litigantes(db, unchecked)
        checked.last_detail_checked_at = datetime.utcnow()
        db.commit()
        sync = admin_dashboard_stats(db, ACCOUNT_RUT)["sync"]
        assert sync["pending_detail"] == 1
        assert sync["checked_24h"] == 1
        assert sync["last_checked_at"] is not None

    def test_with_semaforo_count(self, db, lawyer, court):
        c1 = _make_case(db, lawyer, court, "C-6003-2025", semaforo="rojo")
        c2 = _make_case(db, lawyer, court, "C-6004-2025", semaforo=None)
        _seed_litigantes(db, c1)
        _seed_litigantes(db, c2)
        quality = admin_dashboard_stats(db, ACCOUNT_RUT)["quality"]
        assert quality["total_cases"] == 2
        assert quality["with_semaforo"] == 1

    def test_documents_breakdown(self, db, lawyer, court):
        c = _make_case(db, lawyer, court, "C-6005-2025")
        _seed_litigantes(db, c)
        db.add_all([
            Document(case_id=c.id, status="stored"),
            Document(case_id=c.id, status="stored"),
            Document(case_id=c.id, status="pending"),
            Document(case_id=c.id, status="failed"),
        ])
        db.commit()
        docs = admin_dashboard_stats(db, ACCOUNT_RUT)["documents"]
        assert docs["stored"] == 2
        assert docs["pending"] == 1
        assert docs["failed"] == 1
        assert docs["unavailable"] == 0

    def test_with_movements_and_litigantes(self, db, lawyer, court):
        c1 = _make_case(db, lawyer, court, "C-6006-2025")
        c2 = _make_case(db, lawyer, court, "C-6007-2025")
        _seed_litigantes(db, c1)
        _seed_litigantes(db, c2)
        db.add(Movement(case_id=c1.id, description="Resolución", movement_date=datetime.utcnow()))
        db.commit()
        quality = admin_dashboard_stats(db, ACCOUNT_RUT)["quality"]
        assert quality["with_movements"] == 1
        assert quality["with_litigantes"] == 2

    def test_sin_asignar(self, db, lawyer, court):
        assigned = _make_case(db, lawyer, court, "C-6008-2025")
        _seed_litigantes(db, assigned)  # firm-side abogado present
        _make_case(db, lawyer, court, "C-6009-2025")  # orphan: no litigantes
        db.commit()
        quality = admin_dashboard_stats(db, ACCOUNT_RUT)["quality"]
        assert quality["total_cases"] == 2
        assert quality["sin_asignar"] == 1

    def test_empty_for_unknown_account(self, db):
        result = admin_dashboard_stats(db, "99999999-9")
        assert result["quality"]["total_cases"] == 0
        assert result["sync"]["pending_detail"] == 0


# ---------------------------------------------------------------------------
# Endpoint tests: GET /api/v1/stats/admin
# ---------------------------------------------------------------------------


class TestAdminStatsEndpoint:
    def test_endpoint_returns_200_correct_shape(self, authed_client, db, lawyer, court):
        c = _make_case(db, lawyer, court, "C-6101-2025", semaforo="verde")
        _seed_litigantes(db, c)
        response = authed_client.get("/api/v1/stats/admin")
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) >= {"sync", "documents", "quality"}
        assert set(data["sync"].keys()) >= {
            "last_checked_at", "checked_24h", "pending_detail", "stale_30d"
        }
        assert set(data["documents"].keys()) >= {
            "stored", "pending", "failed", "unavailable"
        }
        assert data["quality"]["total_cases"] == 1

    def test_endpoint_returns_401_without_auth(self, client):
        response = client.get("/api/v1/stats/admin")
        assert response.status_code == 401
