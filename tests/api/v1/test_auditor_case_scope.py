"""Tests for auditor case-visibility scope: auditor must see EVERY study case
across ALL lawyers, not just the firm account's own cases.

Regression coverage: a regular lawyer must still see only their own cases.
"""

import pytest
from datetime import date, datetime, timedelta

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.lawyer import Lawyer

AUDITOR_RUT = "77777777-7"
LAWYER_A_RUT = "88888888-8"
LAWYER_B_RUT = "99999999-9"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auditor(db):
    l = Lawyer(rut=AUDITOR_RUT, name="Auditor User", role="auditor")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def lawyer_a(db):
    l = Lawyer(rut=LAWYER_A_RUT, name="Lawyer A", role="lawyer")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def lawyer_b(db):
    l = Lawyer(rut=LAWYER_B_RUT, name="Lawyer B", role="lawyer")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def court(db):
    obj = Court(code="T1-AUD", name="Juzgado Auditor", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def auditor_headers(auditor):
    tok = create_access_token({"sub": AUDITOR_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def lawyer_a_headers(lawyer_a):
    tok = create_access_token({"sub": LAWYER_A_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


def _make_case(db, lawyer, court, rol, **kwargs):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
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


@pytest.fixture
def case_a(db, lawyer_a, court):
    return _make_case(db, lawyer_a, court, "C-9001-2026", semaforo="rojo")


@pytest.fixture
def case_b(db, lawyer_b, court):
    return _make_case(db, lawyer_b, court, "C-9002-2026", semaforo="verde")


# ---------------------------------------------------------------------------
# GET /api/v1/cases — Causas list
# ---------------------------------------------------------------------------


class TestCasesListScope:
    def test_auditor_sees_cases_from_all_lawyers(
        self, client, case_a, case_b, auditor_headers
    ):
        resp = client.get("/api/v1/cases", headers=auditor_headers)
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert case_a.id in ids
        assert case_b.id in ids

    def test_regular_lawyer_sees_only_own_cases(
        self, client, case_a, case_b, lawyer_a_headers
    ):
        resp = client.get("/api/v1/cases", headers=lawyer_a_headers)
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert case_a.id in ids
        assert case_b.id not in ids


# ---------------------------------------------------------------------------
# GET /api/v1/cases/{case_id} and related detail endpoints
# ---------------------------------------------------------------------------


class TestCaseDetailScope:
    def test_auditor_can_view_case_owned_by_another_lawyer(
        self, client, case_b, auditor_headers
    ):
        resp = client.get(f"/api/v1/cases/{case_b.id}", headers=auditor_headers)
        assert resp.status_code == 200
        assert resp.json()["case"]["id"] == case_b.id

    def test_regular_lawyer_cannot_view_other_lawyers_case(
        self, client, case_b, lawyer_a_headers
    ):
        resp = client.get(f"/api/v1/cases/{case_b.id}", headers=lawyer_a_headers)
        assert resp.status_code == 404

    def test_auditor_can_view_movements_of_another_lawyers_case(
        self, client, case_b, auditor_headers
    ):
        resp = client.get(f"/api/v1/cases/{case_b.id}/movements", headers=auditor_headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/v1/stats/firm — Dashboard
# ---------------------------------------------------------------------------


class TestFirmStatsScope:
    def test_auditor_firm_stats_aggregate_all_lawyers(
        self, client, case_a, case_b, auditor_headers
    ):
        resp = client.get("/api/v1/stats/firm", headers=auditor_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["totals"]["cases"] == 2
        ruts = {row["rut"] for row in data["by_lawyer"]}
        assert LAWYER_A_RUT in ruts
        assert LAWYER_B_RUT in ruts

    def test_auditor_firm_stats_semaforo_spans_all_lawyers(
        self, client, case_a, case_b, auditor_headers
    ):
        resp = client.get("/api/v1/stats/firm", headers=auditor_headers)
        assert resp.status_code == 200
        sem = resp.json()["totals"]["semaforo"]
        assert sem["rojo"] == 1
        assert sem["verde"] == 1


# ---------------------------------------------------------------------------
# GET /api/v1/cases/deadlines/audited — Plazos
# ---------------------------------------------------------------------------


@pytest.fixture
def audited_deadline_a(db, case_a):
    dl = CaseDeadline(
        case_id=case_a.id,
        deadline_type="excepciones_8d",
        legal_basis="art. 459 CPC",
        due_date=date.today() + timedelta(days=2),
        triggered_at=date.today(),
        status="cumplido",
    )
    db.add(dl)
    db.commit()
    db.refresh(dl)
    return dl


@pytest.fixture
def audited_deadline_b(db, case_b):
    dl = CaseDeadline(
        case_id=case_b.id,
        deadline_type="apelacion_5d",
        legal_basis="art. 187/475 CPC",
        due_date=date.today() + timedelta(days=4),
        triggered_at=date.today(),
        status="no_cumplido",
    )
    db.add(dl)
    db.commit()
    db.refresh(dl)
    return dl


class TestAuditedDeadlinesScope:
    def test_auditor_sees_audited_deadlines_from_all_lawyers(
        self, client, audited_deadline_a, audited_deadline_b, auditor_headers
    ):
        resp = client.get("/api/v1/cases/deadlines/audited", headers=auditor_headers)
        assert resp.status_code == 200
        rols = {row["rol"] for row in resp.json()}
        assert "C-9001-2026" in rols
        assert "C-9002-2026" in rols

    def test_regular_lawyer_sees_only_own_audited_deadlines(
        self, client, audited_deadline_a, audited_deadline_b, lawyer_a_headers
    ):
        resp = client.get("/api/v1/cases/deadlines/audited", headers=lawyer_a_headers)
        assert resp.status_code == 200
        rols = {row["rol"] for row in resp.json()}
        assert rols == {"C-9001-2026"}


# ---------------------------------------------------------------------------
# GET /api/v1/cases/{case_id}/deadlines — case deadline detail
# ---------------------------------------------------------------------------


class TestCaseDeadlinesDetailScope:
    def test_auditor_can_view_deadlines_of_another_lawyers_case(
        self, client, case_b, auditor_headers
    ):
        resp = client.get(f"/api/v1/cases/{case_b.id}/deadlines", headers=auditor_headers)
        assert resp.status_code == 200
        assert resp.json()["case_id"] == case_b.id

    def test_regular_lawyer_cannot_view_deadlines_of_other_lawyers_case(
        self, client, case_b, lawyer_a_headers
    ):
        resp = client.get(f"/api/v1/cases/{case_b.id}/deadlines", headers=lawyer_a_headers)
        assert resp.status_code == 404
