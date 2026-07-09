"""Tests for GET /api/v1/calendar — slice 2a of req #5/#11 (calendarización).

Agenda of upcoming/overdue DEADLINES and REVIEW dates across the caller's
scoped cases, sorted by date ascending. Reuses data already denormalized on
``Case`` by ``DeadlineEngine``/``DecisionEngine``: ``next_deadline_at``,
``next_deadline_fatal``, ``next_review_at``, ``recommended_action_code``.
"""

import pytest
from datetime import date, datetime, timedelta

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer

TODAY = date(2026, 7, 9)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    """Pin the endpoint's clock to TODAY for deterministic window assertions."""
    monkeypatch.setattr("app.api.v1.calendar._today_chile", lambda: TODAY)


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Cal Lawyer", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def other_lawyer(db):
    obj = Lawyer(rut="55555555-5", name="Other Lawyer", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def auditor(db):
    obj = Lawyer(rut="77777777-7", name="Auditor User", role="auditor")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-CAL", name="Juzgado Calendario", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_case(db, lawyer, court, rol, **kwargs):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status=kwargs.pop("status", "active"),
        competencia="civil",
        plaintiff=kwargs.pop("plaintiff", "Banco X"),
        defendant=kwargs.pop("defendant", "Deudor Y"),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        **kwargs,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _seed_litigante(db, case, rut, nombre):
    db.add(CaseLitigante(
        case_id=case.id,
        participante="AB.DDO",
        rut=rut,
        persona_type="NATURAL",
        nombre=nombre,
        natural_key=f"{case.id}-{rut}",
    ))
    db.commit()


@pytest.fixture
def authed_client(client, lawyer):
    async def _mock():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock
    yield client
    app.dependency_overrides.pop(get_current_lawyer, None)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_returns_401_without_auth(self, client):
        response = client.get("/api/v1/calendar")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Horizon window
# ---------------------------------------------------------------------------


class TestHorizonWindow:
    def test_deadline_within_horizon_included(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1001-2026",
            next_deadline_at=TODAY + timedelta(days=5),
            next_deadline_fatal=False,
            semaforo="amarillo",
        )
        response = authed_client.get("/api/v1/calendar")
        assert response.status_code == 200
        items = response.json()["items"]
        assert any(i["case_id"] == c.id and i["kind"] == "deadline" for i in items)

    def test_deadline_beyond_horizon_excluded(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-1002-2026",
            next_deadline_at=TODAY + timedelta(days=45),
            next_deadline_fatal=False,
            semaforo="verde",
        )
        response = authed_client.get("/api/v1/calendar?days=30")
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_deadline_exactly_at_horizon_boundary_included(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1003-2026",
            next_deadline_at=TODAY + timedelta(days=30),
            next_deadline_fatal=False,
            semaforo="amarillo",
        )
        response = authed_client.get("/api/v1/calendar?days=30")
        assert response.status_code == 200
        items = response.json()["items"]
        assert any(i["case_id"] == c.id for i in items)


# ---------------------------------------------------------------------------
# include_overdue
# ---------------------------------------------------------------------------


class TestIncludeOverdue:
    def test_include_overdue_false_excludes_past_due(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-1101-2026",
            next_deadline_at=TODAY - timedelta(days=3),
            next_deadline_fatal=True,
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar?include_overdue=false")
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_include_overdue_true_includes_with_overdue_flag(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1102-2026",
            next_deadline_at=TODAY - timedelta(days=3),
            next_deadline_fatal=True,
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar?include_overdue=true")
        assert response.status_code == 200
        items = response.json()["items"]
        item = next(i for i in items if i["case_id"] == c.id)
        assert item["overdue"] is True

    def test_include_overdue_true_but_verde_still_excluded(self, authed_client, db, lawyer, court):
        """A cleared/verde case's past-due deadline is not 'still active' —
        excluded even when include_overdue=true."""
        _make_case(
            db, lawyer, court, "C-1103-2026",
            next_deadline_at=TODAY - timedelta(days=3),
            next_deadline_fatal=False,
            semaforo="verde",
        )
        response = authed_client.get("/api/v1/calendar?include_overdue=true")
        assert response.status_code == 200
        assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# Two items per case + recommended_action resolution
# ---------------------------------------------------------------------------


class TestTwoItemsPerCase:
    def test_case_with_both_dates_yields_two_items(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1201-2026",
            next_deadline_at=TODAY + timedelta(days=2),
            next_deadline_fatal=True,
            next_review_at=TODAY + timedelta(days=4),
            recommended_action_code="oponer_excepciones",
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar")
        assert response.status_code == 200
        items = [i for i in response.json()["items"] if i["case_id"] == c.id]
        kinds = {i["kind"] for i in items}
        assert kinds == {"deadline", "review"}

    def test_review_item_resolves_recommended_action_text_and_urgency(
        self, authed_client, db, lawyer, court
    ):
        c = _make_case(
            db, lawyer, court, "C-1202-2026",
            next_review_at=TODAY + timedelta(days=1),
            recommended_action_code="oponer_excepciones",
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar")
        assert response.status_code == 200
        items = [i for i in response.json()["items"] if i["case_id"] == c.id]
        assert len(items) == 1
        review = items[0]
        assert review["kind"] == "review"
        assert review["recommended_action"] == "Oponer excepciones (escrito de oposición)"
        assert review["urgency"] == "critica"

    def test_review_item_with_unknown_code_resolves_to_none(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1203-2026",
            next_review_at=TODAY + timedelta(days=1),
            recommended_action_code="not_a_real_code",
            semaforo="amarillo",
        )
        response = authed_client.get("/api/v1/calendar")
        review = next(i for i in response.json()["items"] if i["case_id"] == c.id)
        assert review["recommended_action"] is None
        assert review["urgency"] is None


# ---------------------------------------------------------------------------
# Fatal flag + summary
# ---------------------------------------------------------------------------


class TestFatalAndSummary:
    def test_fatal_flag_on_deadline_item(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1301-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            next_deadline_fatal=True,
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar")
        item = next(i for i in response.json()["items"] if i["case_id"] == c.id)
        assert item["fatal"] is True

    def test_summary_counts(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-1302-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            next_deadline_fatal=True,
            semaforo="rojo",
        )
        _make_case(
            db, lawyer, court, "C-1303-2026",
            next_deadline_at=TODAY - timedelta(days=2),
            next_deadline_fatal=False,
            semaforo="rojo",
        )
        _make_case(
            db, lawyer, court, "C-1304-2026",
            next_deadline_at=TODAY + timedelta(days=3),
            next_deadline_fatal=False,
            semaforo="amarillo",
        )
        response = authed_client.get("/api/v1/calendar")
        summary = response.json()["summary"]
        assert summary["total"] == 3
        assert summary["overdue_count"] == 1
        assert summary["fatal_count"] == 1


# ---------------------------------------------------------------------------
# Sorting + caratulado
# ---------------------------------------------------------------------------


class TestSortingAndCaratulado:
    def test_items_sorted_by_date_ascending(self, authed_client, db, lawyer, court):
        c_late = _make_case(
            db, lawyer, court, "C-1401-2026",
            next_deadline_at=TODAY + timedelta(days=10),
            semaforo="amarillo",
        )
        c_early = _make_case(
            db, lawyer, court, "C-1402-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar")
        items = response.json()["items"]
        dates = [i["date"] for i in items]
        assert dates == sorted(dates)
        assert items[0]["case_id"] == c_early.id
        assert items[-1]["case_id"] == c_late.id

    def test_caratulado_reconstructed_from_plaintiff_defendant(self, authed_client, db, lawyer, court):
        c = _make_case(
            db, lawyer, court, "C-1403-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="amarillo",
            plaintiff="BANCO ACME",
            defendant="JUAN PEREZ",
        )
        response = authed_client.get("/api/v1/calendar")
        item = next(i for i in response.json()["items"] if i["case_id"] == c.id)
        assert item["caratulado"] == "BANCO ACME/JUAN PEREZ"
        assert item["rol"] == "C-1403-2026"
        assert item["court_name"] == court.name


# ---------------------------------------------------------------------------
# Archived cases excluded
# ---------------------------------------------------------------------------


class TestArchivedExcluded:
    def test_archived_case_excluded(self, authed_client, db, lawyer, court):
        _make_case(
            db, lawyer, court, "C-1501-2026",
            status="archived",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        response = authed_client.get("/api/v1/calendar")
        assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


class TestScoping:
    def test_regular_lawyer_sees_only_own_cases(
        self, client, db, lawyer, other_lawyer, court
    ):
        from app.core.security import create_access_token

        own_case = _make_case(
            db, lawyer, court, "C-1601-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        _seed_litigante(db, own_case, lawyer.rut, lawyer.name)

        other_case = _make_case(
            db, other_lawyer, court, "C-1602-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        _seed_litigante(db, other_case, other_lawyer.rut, other_lawyer.name)

        token = create_access_token({"sub": lawyer.rut}, expires_delta=timedelta(minutes=30))
        response = client.get(
            "/api/v1/calendar", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        case_ids = {i["case_id"] for i in response.json()["items"]}
        assert own_case.id in case_ids
        assert other_case.id not in case_ids

    def test_auditor_sees_cases_from_all_lawyers(
        self, client, db, lawyer, other_lawyer, auditor, court
    ):
        from app.core.security import create_access_token

        case_a = _make_case(
            db, lawyer, court, "C-1603-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        _seed_litigante(db, case_a, lawyer.rut, lawyer.name)

        case_b = _make_case(
            db, other_lawyer, court, "C-1604-2026",
            next_deadline_at=TODAY + timedelta(days=1),
            semaforo="rojo",
        )
        _seed_litigante(db, case_b, other_lawyer.rut, other_lawyer.name)

        token = create_access_token({"sub": auditor.rut}, expires_delta=timedelta(minutes=30))
        response = client.get(
            "/api/v1/calendar", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        case_ids = {i["case_id"] for i in response.json()["items"]}
        assert case_a.id in case_ids
        assert case_b.id in case_ids
