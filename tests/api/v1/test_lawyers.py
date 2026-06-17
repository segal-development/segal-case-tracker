"""Tests for firm_roster helper and GET /api/v1/lawyers endpoint."""

import pytest
from datetime import datetime
from fastapi.testclient import TestClient

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import firm_roster, case_ids_for_abogado


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

ACCOUNT_RUT = "11111111-1"
FIRM_LAWYER_RUT = "22222222-2"
OPPOSING_RUT = "33333333-3"


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=ACCOUNT_RUT, name="Account Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-LW", name="Juzgado Lawyers", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def case1(db, lawyer, court):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-9001-2025",
        status="active",
        competencia="civil",
        plaintiff="BANCO DEMANDANTE",
        defendant="DEUDOR DDO",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def case2(db, lawyer, court):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-9002-2025",
        status="active",
        competencia="civil",
        plaintiff="BANCO DEMANDANTE 2",
        defendant="DEUDOR DDO 2",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _seed_litigantes(db, case, account_rut, firm_rut, firm_nombre, opposing_rut):
    """Seed: account as AB.DDO, firm lawyer as AB.DDO, opposing as AB.DTE."""
    lits = [
        CaseLitigante(
            case_id=case.id,
            participante="AB.DDO",
            rut=account_rut,
            persona_type="NATURAL",
            nombre="Account Lawyer",
            natural_key=f"{case.id}-account",
        ),
        CaseLitigante(
            case_id=case.id,
            participante="AB.DDO",
            rut=firm_rut,
            persona_type="NATURAL",
            nombre=firm_nombre,
            natural_key=f"{case.id}-firm",
        ),
        CaseLitigante(
            case_id=case.id,
            participante="AB.DTE",
            rut=opposing_rut,
            persona_type="NATURAL",
            nombre="Opposing Lawyer",
            natural_key=f"{case.id}-opposing",
        ),
    ]
    db.add_all(lits)
    db.commit()


# ---------------------------------------------------------------------------
# Unit tests: firm_roster
# ---------------------------------------------------------------------------


class TestFirmRoster:
    def test_includes_co_side_abogado(self, db, case1):
        _seed_litigantes(
            db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT,
            "Firm Lawyer", OPPOSING_RUT
        )
        roster = firm_roster(db, ACCOUNT_RUT)
        ruts = [r["rut"] for r in roster]
        assert FIRM_LAWYER_RUT in ruts

    def test_excludes_opposing_abogado(self, db, case1):
        _seed_litigantes(
            db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT,
            "Firm Lawyer", OPPOSING_RUT
        )
        roster = firm_roster(db, ACCOUNT_RUT)
        ruts = [r["rut"] for r in roster]
        assert OPPOSING_RUT not in ruts

    def test_case_count_correct(self, db, case1, case2):
        # Firm lawyer appears in both cases
        _seed_litigantes(db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm Lawyer", OPPOSING_RUT)
        _seed_litigantes(db, case2, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm Lawyer", OPPOSING_RUT)
        roster = firm_roster(db, ACCOUNT_RUT)
        firm_entry = next(r for r in roster if r["rut"] == FIRM_LAWYER_RUT)
        assert firm_entry["case_count"] == 2

    def test_name_stripped_of_trailing_paren(self, db, case1):
        _seed_litigantes(
            db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT,
            "Firm Lawyer (Poder Notarial)", OPPOSING_RUT
        )
        roster = firm_roster(db, ACCOUNT_RUT)
        firm_entry = next(r for r in roster if r["rut"] == FIRM_LAWYER_RUT)
        assert firm_entry["nombre"] == "Firm Lawyer"

    def test_dotted_rut_matched_via_normalize(self, db, case1):
        """Firm lawyer stored with dotted RUT is still matched correctly."""
        dotted_rut = "22.222.222-2"  # dots; normalizes to FIRM_LAWYER_RUT
        lits = [
            CaseLitigante(
                case_id=case1.id,
                participante="AB.DDO",
                rut=ACCOUNT_RUT,
                persona_type="NATURAL",
                nombre="Account Lawyer",
                natural_key=f"{case1.id}-acct-dot",
            ),
            CaseLitigante(
                case_id=case1.id,
                participante="AB.DDO",
                rut=dotted_rut,
                persona_type="NATURAL",
                nombre="Dotted Firm Lawyer",
                natural_key=f"{case1.id}-firm-dot",
            ),
        ]
        db.add_all(lits)
        db.commit()
        roster = firm_roster(db, ACCOUNT_RUT)
        ruts = [r["rut"] for r in roster]
        # The returned rut should be normalized (no dots)
        assert FIRM_LAWYER_RUT in ruts

    def test_returns_empty_for_unknown_account_rut(self, db):
        roster = firm_roster(db, "99999999-9")
        assert roster == []

    def test_sorted_by_case_count_desc(self, db, case1, case2, court):
        """Lawyer on 2 cases ranks above lawyer on 1 case."""
        OTHER_RUT = "44444444-4"
        _seed_litigantes(db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm A", OPPOSING_RUT)
        _seed_litigantes(db, case2, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm A", OPPOSING_RUT)
        # Add OTHER_RUT only to case1
        db.add(CaseLitigante(
            case_id=case1.id,
            participante="AB.DDO",
            rut=OTHER_RUT,
            persona_type="NATURAL",
            nombre="Firm B",
            natural_key=f"{case1.id}-other",
        ))
        db.commit()
        roster = firm_roster(db, ACCOUNT_RUT)
        ruts = [r["rut"] for r in roster]
        assert ruts.index(FIRM_LAWYER_RUT) < ruts.index(OTHER_RUT)


# ---------------------------------------------------------------------------
# Unit tests: case_ids_for_abogado
# ---------------------------------------------------------------------------


class TestCaseIdsForAbogado:
    def test_returns_case_where_abogado_is_firm_side(self, db, case1):
        _seed_litigantes(db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm Lawyer", OPPOSING_RUT)
        ids = case_ids_for_abogado(db, ACCOUNT_RUT, FIRM_LAWYER_RUT)
        assert case1.id in ids

    def test_excludes_opposing_abogado(self, db, case1):
        _seed_litigantes(db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm Lawyer", OPPOSING_RUT)
        ids = case_ids_for_abogado(db, ACCOUNT_RUT, OPPOSING_RUT)
        assert case1.id not in ids

    def test_returns_empty_for_unknown_account(self, db, case1):
        ids = case_ids_for_abogado(db, "99999999-9", FIRM_LAWYER_RUT)
        assert ids == set()


# ---------------------------------------------------------------------------
# Endpoint tests: GET /api/v1/lawyers
# ---------------------------------------------------------------------------


@pytest.fixture
def authed_client(client, lawyer):
    async def _mock_get_current_lawyer():
        return {"sub": str(lawyer.id)}
    app.dependency_overrides[get_current_lawyer] = _mock_get_current_lawyer
    yield client


class TestListFirmLawyers:
    def test_returns_200_with_roster(self, authed_client, db, case1, lawyer):
        _seed_litigantes(db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm Lawyer (Poder)", OPPOSING_RUT)
        response = authed_client.get("/api/v1/lawyers")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert any(item["rut"] == FIRM_LAWYER_RUT for item in data)

    def test_nombre_cleaned_in_response(self, authed_client, db, case1):
        _seed_litigantes(db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm Lawyer (Poder Notarial)", OPPOSING_RUT)
        response = authed_client.get("/api/v1/lawyers")
        data = response.json()
        firm_entry = next(item for item in data if item["rut"] == FIRM_LAWYER_RUT)
        assert firm_entry["nombre"] == "Firm Lawyer"

    def test_returns_401_without_auth(self, client):
        response = client.get("/api/v1/lawyers")
        assert response.status_code == 401

    def test_returns_empty_list_when_no_cases(self, authed_client):
        response = authed_client.get("/api/v1/lawyers")
        assert response.status_code == 200
        assert response.json() == []
