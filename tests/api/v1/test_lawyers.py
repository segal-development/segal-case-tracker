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

    def test_includes_case_synced_under_different_lawyer_id(self, db, lawyer, court):
        """Firm-wide attribution: a case Case.lawyer_id != the account's own
        id (synced under a different lawyer's account) still surfaces the
        account as a litigante-abogado co-side firm lawyer."""
        other_lawyer = Lawyer(rut="55555555-5", name="Other Syncer")
        db.add(other_lawyer)
        db.commit()
        db.refresh(other_lawyer)

        case = Case(
            lawyer_id=other_lawyer.id,
            court_id=court.id,
            rol="C-9010-2025",
            status="active",
            competencia="civil",
            plaintiff="BANCO DEMANDANTE",
            defendant="DEUDOR DDO",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(case)
        db.commit()
        db.refresh(case)

        _seed_litigantes(
            db, case, ACCOUNT_RUT, FIRM_LAWYER_RUT,
            "Firm Lawyer", OPPOSING_RUT,
        )

        roster = firm_roster(db, ACCOUNT_RUT)
        ruts = [r["rut"] for r in roster]
        assert FIRM_LAWYER_RUT in ruts

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

    def test_includes_opposing_side_abogado_of_record(self, db, case1):
        """Firm-wide fix: OPPOSING_RUT is seeded as AB.DTE — a genuine
        abogado-of-record on the opposing side of case1. Querying for
        OPPOSING_RUT specifically now correctly includes case1, since
        attribution is entirely about whether the TARGET is an
        abogado-of-record, independent of the viewing account's own side.
        (Previously this was excluded by the account-side gate, which
        conflated "not co-side with the viewing account" with "not an
        abogado of record" — see the bugfix this test replaces.)"""
        _seed_litigantes(db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm Lawyer", OPPOSING_RUT)
        ids = case_ids_for_abogado(db, ACCOUNT_RUT, OPPOSING_RUT)
        assert case1.id in ids

    def test_returns_empty_for_unknown_account(self, db, case1):
        ids = case_ids_for_abogado(db, "99999999-9", FIRM_LAWYER_RUT)
        assert ids == set()

    def test_returns_case_regardless_of_lawyer_id_owner(self, db, lawyer, court):
        """Firm-wide attribution: the case's Case.lawyer_id belongs to a
        different (syncing) lawyer, yet case_ids_for_abogado still resolves
        it via case_litigantes."""
        other_lawyer = Lawyer(rut="66666666-6", name="Other Syncer")
        db.add(other_lawyer)
        db.commit()
        db.refresh(other_lawyer)

        case = Case(
            lawyer_id=other_lawyer.id,
            court_id=court.id,
            rol="C-9011-2025",
            status="active",
            competencia="civil",
            plaintiff="BANCO DEMANDANTE",
            defendant="DEUDOR DDO",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(case)
        db.commit()
        db.refresh(case)

        _seed_litigantes(db, case, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm Lawyer", OPPOSING_RUT)

        ids = case_ids_for_abogado(db, ACCOUNT_RUT, FIRM_LAWYER_RUT)
        assert case.id in ids

    def test_includes_case_where_viewing_account_is_not_a_litigante(self, db, case1, case2):
        """Firm-wide fix: the target abogado (FIRM_LAWYER_RUT) is abogado-of-record
        on case2, but the VIEWING account is not a litigante on case2 at all.
        Dropping the account-side gate means the viewing account still sees
        case2 when asking specifically about FIRM_LAWYER_RUT's caseload —
        previously this case was hidden because the account had no side
        resolved on it."""
        _seed_litigantes(db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm Lawyer", OPPOSING_RUT)
        db.add(CaseLitigante(
            case_id=case2.id,
            participante="AB.DDO",
            rut=FIRM_LAWYER_RUT,
            persona_type="NATURAL",
            nombre="Firm Lawyer",
            natural_key=f"{case2.id}-firm-only",
        ))
        db.commit()

        ids = case_ids_for_abogado(db, ACCOUNT_RUT, FIRM_LAWYER_RUT)
        assert case2.id in ids

    def test_excludes_case_where_target_is_not_abogado_of_record(self, db, case2):
        """Target RUT appears on the case only as a party (DDO, not AB.DDO) —
        not an abogado-of-record litigante — so the case is excluded."""
        db.add(CaseLitigante(
            case_id=case2.id,
            participante="DDO",
            rut=FIRM_LAWYER_RUT,
            persona_type="NATURAL",
            nombre="Firm Lawyer as party",
            natural_key=f"{case2.id}-party-only",
        ))
        db.commit()

        ids = case_ids_for_abogado(db, ACCOUNT_RUT, FIRM_LAWYER_RUT)
        assert case2.id not in ids

    def test_self_query_returns_all_own_abogado_cases_no_regression(self, db, case1, case2):
        """No regression: querying for self (account == target abogado) still
        returns every case where the account is abogado-of-record, matching
        the (lawyer.rut, lawyer.rut) usage in deps.py/stats.py."""
        _seed_litigantes(db, case1, ACCOUNT_RUT, FIRM_LAWYER_RUT, "Firm Lawyer", OPPOSING_RUT)
        db.add(CaseLitigante(
            case_id=case2.id,
            participante="AB.DTE",
            rut=ACCOUNT_RUT,
            persona_type="NATURAL",
            nombre="Account Lawyer",
            natural_key=f"{case2.id}-account-solo",
        ))
        db.commit()

        ids = case_ids_for_abogado(db, ACCOUNT_RUT, ACCOUNT_RUT)
        assert {case1.id, case2.id} <= ids


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
