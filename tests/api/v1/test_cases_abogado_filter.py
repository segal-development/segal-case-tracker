"""Tests for GET /api/v1/cases?abogado_rut= filter."""

import pytest
from datetime import datetime
from fastapi.testclient import TestClient

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer


ACCOUNT_RUT = "11111111-1"
FIRM_LAWYER_RUT = "22222222-2"
OTHER_FIRM_RUT = "44444444-4"


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=ACCOUNT_RUT, name="Account Lawyer AF")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-AF", name="Juzgado AF", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _make_case(db, lawyer, court, rol):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        plaintiff="P",
        defendant="D",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _add_litigante(db, case_id, participante, rut, natural_key):
    lit = CaseLitigante(
        case_id=case_id,
        participante=participante,
        rut=rut,
        persona_type="NATURAL",
        nombre="Test",
        natural_key=natural_key,
    )
    db.add(lit); db.commit()


@pytest.fixture
def authed_client(client, lawyer):
    async def _mock():
        return {"sub": str(lawyer.id)}
    app.dependency_overrides[get_current_lawyer] = _mock
    yield client


@pytest.fixture
def two_cases(db, lawyer, court):
    """case_a: firm lawyer is co-side. case_b: firm lawyer NOT present."""
    case_a = _make_case(db, lawyer, court, "C-8001-2025")
    case_b = _make_case(db, lawyer, court, "C-8002-2025")

    # case_a: account=AB.DDO, firm=AB.DDO
    _add_litigante(db, case_a.id, "AB.DDO", ACCOUNT_RUT, f"{case_a.id}-acct")
    _add_litigante(db, case_a.id, "AB.DDO", FIRM_LAWYER_RUT, f"{case_a.id}-firm")

    # case_b: account=AB.DDO, firm NOT present
    _add_litigante(db, case_b.id, "AB.DDO", ACCOUNT_RUT, f"{case_b.id}-acct")

    return case_a, case_b


class TestCasesAbogadoFilter:
    def test_without_filter_returns_all_cases(self, authed_client, two_cases):
        case_a, case_b = two_cases
        resp = authed_client.get("/api/v1/cases")
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert case_a.id in ids
        assert case_b.id in ids

    def test_with_abogado_rut_returns_only_matching_cases(self, authed_client, two_cases):
        case_a, case_b = two_cases
        resp = authed_client.get(f"/api/v1/cases?abogado_rut={FIRM_LAWYER_RUT}")
        assert resp.status_code == 200
        data = resp.json()
        ids = {item["id"] for item in data["items"]}
        assert case_a.id in ids
        assert case_b.id not in ids

    def test_abogado_rut_with_no_match_returns_empty(self, authed_client, two_cases):
        resp = authed_client.get("/api/v1/cases?abogado_rut=99999999-9")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_total_reflects_filtered_count(self, authed_client, two_cases):
        case_a, case_b = two_cases
        resp = authed_client.get(f"/api/v1/cases?abogado_rut={FIRM_LAWYER_RUT}")
        data = resp.json()
        assert data["total"] == 1

    def test_existing_filters_still_work_alongside_abogado_rut(self, authed_client, two_cases):
        """competencia filter + abogado_rut both applied."""
        case_a, _ = two_cases
        resp = authed_client.get(
            f"/api/v1/cases?abogado_rut={FIRM_LAWYER_RUT}&competencia=civil"
        )
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert case_a.id in ids


class TestStablePagination:
    def test_criticidad_pagination_no_dupes_or_omissions(
        self, authed_client, db, lawyer, court
    ):
        """All cases tie under the criticidad sort (next_deadline_at is NULL), so
        without a stable id tiebreaker pagination could duplicate or omit rows.
        Every case must appear exactly once across all pages."""
        n = 7
        for i in range(n):
            _make_case(db, lawyer, court, f"C-93{i:02d}-2025")

        seen: list[int] = []
        for page in range(1, n + 2):  # per_page=2 → several pages (+ empties)
            resp = authed_client.get(
                f"/api/v1/cases?sort_by=criticidad&per_page=2&page={page}"
            )
            assert resp.status_code == 200
            seen.extend(item["id"] for item in resp.json()["items"])

        assert len(seen) == len(set(seen)), "pagination returned duplicate case ids"
        assert len(set(seen)) == n, "pagination omitted cases"
