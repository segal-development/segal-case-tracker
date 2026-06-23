"""Tests for GET /api/v1/stats/me — per-lawyer productivity endpoint."""

import calendar
import pytest
from datetime import datetime, timedelta

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.goal import Goal
from app.models.lawyer import Lawyer


ACCOUNT_RUT = "11111111-1"
ACCOUNT_NOMBRE = "Test Lawyer"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=ACCOUNT_RUT, name=ACCOUNT_NOMBRE)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-ME", name="Juzgado Me Stats", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def authed_client(client, lawyer):
    async def _mock():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock
    yield client


def _make_case(db, lawyer, court, rol, semaforo=None, last_movement_at=None, procedural_state=None):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        semaforo=semaforo,
        last_movement_at=last_movement_at,
        procedural_state=procedural_state,
        plaintiff="BANCO DTE",
        defendant="DEUDOR DDO",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _seed_self_litigante(db, case, account_rut=ACCOUNT_RUT):
    """Seed the account holder as AB.DDO on the given case."""
    suffix = str(case.id)
    lit = CaseLitigante(
        case_id=case.id,
        participante="AB.DDO",
        rut=account_rut,
        persona_type="NATURAL",
        nombre=ACCOUNT_NOMBRE,
        natural_key=f"{case.id}-self-{suffix}",
    )
    db.add(lit)
    db.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMyStatsEndpoint:
    def test_case_count_and_semaforo(self, authed_client, db, lawyer, court):
        c1 = _make_case(db, lawyer, court, "C-9001-2025", semaforo="rojo")
        c2 = _make_case(db, lawyer, court, "C-9002-2025", semaforo="verde")
        _seed_self_litigante(db, c1)
        _seed_self_litigante(db, c2)

        r = authed_client.get("/api/v1/stats/me")
        assert r.status_code == 200
        data = r.json()
        assert data["rut"] == ACCOUNT_RUT
        assert data["case_count"] == 2
        assert data["rojo"] == 1
        assert data["verde"] == 1
        assert data["amarillo"] == 0

    def test_stale_count(self, authed_client, db, lawyer, court):
        fresh = _make_case(db, lawyer, court, "C-9003-2025",
                           last_movement_at=datetime.utcnow() - timedelta(days=10))
        stale = _make_case(db, lawyer, court, "C-9004-2025",
                           last_movement_at=datetime.utcnow() - timedelta(days=40))
        none_case = _make_case(db, lawyer, court, "C-9005-2025", last_movement_at=None)
        _seed_self_litigante(db, fresh)
        _seed_self_litigante(db, stale)
        _seed_self_litigante(db, none_case)

        r = authed_client.get("/api/v1/stats/me")
        assert r.status_code == 200
        assert r.json()["stale"] == 2

    def test_actividad_mes_this_month_only(self, authed_client, db, lawyer, court):
        now = datetime.utcnow()
        this_month = now.replace(day=1)
        old = datetime(now.year - 1, now.month, 1)

        c_this = _make_case(db, lawyer, court, "C-9006-2025", last_movement_at=this_month)
        c_old = _make_case(db, lawyer, court, "C-9007-2025", last_movement_at=old)
        _seed_self_litigante(db, c_this)
        _seed_self_litigante(db, c_old)

        r = authed_client.get("/api/v1/stats/me")
        assert r.status_code == 200
        assert r.json()["actividad_mes"] == 1

    def test_proyeccion_mes_formula(self, authed_client, db, lawyer, court):
        now = datetime.utcnow()
        this_month = now.replace(day=1)

        c = _make_case(db, lawyer, court, "C-9008-2025", last_movement_at=this_month)
        _seed_self_litigante(db, c)

        r = authed_client.get("/api/v1/stats/me")
        assert r.status_code == 200
        data = r.json()

        actividad_mes = data["actividad_mes"]
        day = now.day
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        expected = round(actividad_mes / day * days_in_month) if day > 0 else actividad_mes
        assert data["proyeccion_mes"] == expected

    def test_meta_from_goal(self, authed_client, db, lawyer, court):
        c = _make_case(db, lawyer, court, "C-9009-2025")
        _seed_self_litigante(db, c)
        goal = Goal(key="monthly_productivity", value=100, updated_at=datetime.utcnow())
        db.add(goal)
        db.commit()

        r = authed_client.get("/api/v1/stats/me")
        assert r.status_code == 200
        assert r.json()["meta"] == 100

    def test_meta_null_when_unset(self, authed_client, db, lawyer, court):
        c = _make_case(db, lawyer, court, "C-9010-2025")
        _seed_self_litigante(db, c)

        r = authed_client.get("/api/v1/stats/me")
        assert r.status_code == 200
        assert r.json()["meta"] is None

    def test_no_cases_returns_zeros(self, authed_client, db, lawyer):
        """Lawyer with no civil cases — all counts should be 0."""
        r = authed_client.get("/api/v1/stats/me")
        assert r.status_code == 200
        data = r.json()
        assert data["case_count"] == 0
        assert data["rojo"] == 0
        assert data["amarillo"] == 0
        assert data["verde"] == 0
        assert data["otros"] == 0
        assert data["stale"] == 0
        assert data["actividad_mes"] == 0
        assert data["proyeccion_mes"] == 0

    def test_unauthenticated_returns_401(self, client):
        r = client.get("/api/v1/stats/me")
        assert r.status_code == 401
