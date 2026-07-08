"""Tests for firm_risk_board helper and GET /api/v1/stats/risk-board endpoint.

Firm-wide exposure/risk aggregation for the AUDITOR dashboard: rolls up
denormalized Case risk flags (semaforo, abandono_disponible,
prescripcion_cumplida, en_apremio, next_deadline_fatal/next_deadline_at)
that today are only visible per-case.
"""

import pytest
from datetime import date, datetime, timedelta

from app.core.security import create_access_token
from app.main import app
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import firm_risk_board

ACCOUNT_RUT = "16021492-9"
LAWYER_A_RUT = "11111111-1"
LAWYER_B_RUT = "22222222-2"
REGULAR_LAWYER_RUT = "33333333-3"
AUDITOR_RUT = "44444444-4"
ADMIN_RUT = "55555555-5"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def firm_account(db):
    obj = Lawyer(rut=ACCOUNT_RUT, name="Firm Account", role="admin")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-RB", name="1er Juzgado Civil de Santiago", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_case(
    db,
    lawyer,
    court,
    rol,
    *,
    semaforo=None,
    abandono_disponible=False,
    prescripcion_cumplida=False,
    en_apremio=False,
    next_deadline_fatal=False,
    next_deadline_at=None,
    procedural_state=None,
    status="active",
    plaintiff="BANCO DEMANDANTE",
    defendant="DEUDOR DDO",
):
    obj = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status=status,
        competencia="civil",
        semaforo=semaforo,
        abandono_disponible=abandono_disponible,
        prescripcion_cumplida=prescripcion_cumplida,
        en_apremio=en_apremio,
        next_deadline_fatal=next_deadline_fatal,
        next_deadline_at=next_deadline_at,
        procedural_state=procedural_state,
        plaintiff=plaintiff,
        defendant=defendant,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _seed_abogado(db, case, rut, nombre, participante="AB.DDO"):
    suffix = case.rol.replace("-", "")
    lit = CaseLitigante(
        case_id=case.id,
        participante=participante,
        rut=rut,
        persona_type="NATURAL",
        nombre=nombre,
        natural_key=f"{case.id}-{rut}-{suffix}",
    )
    db.add(lit)
    db.commit()
    return lit


# ---------------------------------------------------------------------------
# Unit tests: firm_risk_board — semaforo
# ---------------------------------------------------------------------------


class TestFirmRiskBoardSemaforo:
    def test_semaforo_counts_correct(self, db, firm_account, court):
        c1 = _make_case(db, firm_account, court, "C-9001-2025", semaforo="rojo")
        c2 = _make_case(db, firm_account, court, "C-9002-2025", semaforo="rojo")
        c3 = _make_case(db, firm_account, court, "C-9003-2025", semaforo="amarillo")
        c4 = _make_case(db, firm_account, court, "C-9004-2025", semaforo="verde")
        for c in (c1, c2, c3, c4):
            _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        assert result["semaforo"] == {"rojo": 2, "amarillo": 1, "verde": 1, "gris": 0}

    def test_semaforo_unknown_or_none_becomes_gris(self, db, firm_account, court):
        c1 = _make_case(db, firm_account, court, "C-9005-2025", semaforo=None)
        c2 = _make_case(db, firm_account, court, "C-9006-2025", semaforo="desconocido")
        for c in (c1, c2):
            _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        assert result["semaforo"]["gris"] == 2

    def test_total_matches_active_civil_case_count(self, db, firm_account, court):
        c1 = _make_case(db, firm_account, court, "C-9007-2025")
        c2 = _make_case(db, firm_account, court, "C-9008-2025")
        _seed_abogado(db, c1, LAWYER_A_RUT, "Lawyer A")
        _seed_abogado(db, c2, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        assert result["total"] == 2


# ---------------------------------------------------------------------------
# Unit tests: firm_risk_board — riesgo (exposure flags)
# ---------------------------------------------------------------------------


class TestFirmRiskBoardRiesgo:
    def test_abandono_disponible_count(self, db, firm_account, court):
        c1 = _make_case(db, firm_account, court, "C-9101-2025", abandono_disponible=True)
        c2 = _make_case(db, firm_account, court, "C-9102-2025", abandono_disponible=False)
        for c in (c1, c2):
            _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        assert result["riesgo"]["abandono_disponible"] == 1

    def test_prescripcion_cumplida_count(self, db, firm_account, court):
        c1 = _make_case(db, firm_account, court, "C-9103-2025", prescripcion_cumplida=True)
        c2 = _make_case(db, firm_account, court, "C-9104-2025", prescripcion_cumplida=True)
        c3 = _make_case(db, firm_account, court, "C-9105-2025", prescripcion_cumplida=False)
        for c in (c1, c2, c3):
            _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        assert result["riesgo"]["prescripcion_cumplida"] == 2

    def test_en_apremio_count(self, db, firm_account, court):
        c1 = _make_case(db, firm_account, court, "C-9106-2025", en_apremio=True)
        c2 = _make_case(db, firm_account, court, "C-9107-2025", en_apremio=False)
        c3 = _make_case(db, firm_account, court, "C-9108-2025", en_apremio=False)
        for c in (c1, c2, c3):
            _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        assert result["riesgo"]["en_apremio"] == 1

    def test_plazo_fatal_proximo_vs_vencido_split(self, db, firm_account, court):
        today = date.today()
        proximo = _make_case(
            db, firm_account, court, "C-9109-2025",
            next_deadline_fatal=True, next_deadline_at=today + timedelta(days=3),
        )
        vencido = _make_case(
            db, firm_account, court, "C-9110-2025",
            next_deadline_fatal=True, next_deadline_at=today - timedelta(days=1),
        )
        today_itself = _make_case(
            db, firm_account, court, "C-9111-2025",
            next_deadline_fatal=True, next_deadline_at=today,
        )
        non_fatal = _make_case(
            db, firm_account, court, "C-9112-2025",
            next_deadline_fatal=False, next_deadline_at=today - timedelta(days=5),
        )
        for c in (proximo, vencido, today_itself, non_fatal):
            _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        # today counts as "proximo" (>= today)
        assert result["riesgo"]["plazo_fatal_proximo"] == 2
        assert result["riesgo"]["plazo_fatal_vencido"] == 1

    def test_plazo_fatal_flags_null_next_deadline_at_does_not_crash(self, db, firm_account, court):
        c = _make_case(
            db, firm_account, court, "C-9113-2025",
            next_deadline_fatal=True, next_deadline_at=None,
        )
        _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        assert result["riesgo"]["plazo_fatal_proximo"] == 0
        assert result["riesgo"]["plazo_fatal_vencido"] == 0


# ---------------------------------------------------------------------------
# Unit tests: firm_risk_board — by_lawyer (litigante attribution)
# ---------------------------------------------------------------------------


class TestFirmRiskBoardByLawyer:
    def test_by_lawyer_attributes_via_litigantes(self, db, firm_account, court):
        c1 = _make_case(db, firm_account, court, "C-9201-2025", semaforo="rojo")
        c2 = _make_case(db, firm_account, court, "C-9202-2025", semaforo="verde")
        _seed_abogado(db, c1, LAWYER_A_RUT, "Lawyer A")
        _seed_abogado(db, c2, LAWYER_B_RUT, "Lawyer B")
        result = firm_risk_board(db, ACCOUNT_RUT)
        ruts = {row["rut"] for row in result["by_lawyer"]}
        assert ruts == {LAWYER_A_RUT, LAWYER_B_RUT}
        row_a = next(r for r in result["by_lawyer"] if r["rut"] == LAWYER_A_RUT)
        assert row_a["rojo"] == 1
        assert row_a["total"] == 1

    def test_case_with_two_abogados_counts_for_both(self, db, firm_account, court):
        c1 = _make_case(db, firm_account, court, "C-9203-2025", semaforo="rojo", en_apremio=True)
        _seed_abogado(db, c1, LAWYER_A_RUT, "Lawyer A", participante="AB.DDO")
        _seed_abogado(db, c1, LAWYER_B_RUT, "Lawyer B", participante="AB.DTE")
        result = firm_risk_board(db, ACCOUNT_RUT)
        ruts = {row["rut"] for row in result["by_lawyer"]}
        assert LAWYER_A_RUT in ruts
        assert LAWYER_B_RUT in ruts
        for rut in (LAWYER_A_RUT, LAWYER_B_RUT):
            row = next(r for r in result["by_lawyer"] if r["rut"] == rut)
            assert row["rojo"] == 1
            assert row["en_apremio"] == 1
            assert row["total"] == 1

    def test_by_lawyer_sorted_by_rojo_desc(self, db, firm_account, court):
        c1 = _make_case(db, firm_account, court, "C-9204-2025", semaforo="rojo")
        c2 = _make_case(db, firm_account, court, "C-9205-2025", semaforo="rojo")
        c3 = _make_case(db, firm_account, court, "C-9206-2025", semaforo="verde")
        _seed_abogado(db, c1, LAWYER_A_RUT, "Lawyer A")
        _seed_abogado(db, c2, LAWYER_A_RUT, "Lawyer A")
        _seed_abogado(db, c3, LAWYER_B_RUT, "Lawyer B")
        result = firm_risk_board(db, ACCOUNT_RUT)
        ruts_ordered = [row["rut"] for row in result["by_lawyer"]]
        assert ruts_ordered.index(LAWYER_A_RUT) < ruts_ordered.index(LAWYER_B_RUT)


# ---------------------------------------------------------------------------
# Unit tests: firm_risk_board — top_critical ordering
# ---------------------------------------------------------------------------


class TestFirmRiskBoardTopCritical:
    def test_rojo_ranks_before_non_rojo(self, db, firm_account, court):
        today = date.today()
        rojo_case = _make_case(
            db, firm_account, court, "C-9301-2025", semaforo="rojo",
            next_deadline_at=today + timedelta(days=10),
        )
        verde_case = _make_case(
            db, firm_account, court, "C-9302-2025", semaforo="verde",
            next_deadline_at=today + timedelta(days=1),
        )
        _seed_abogado(db, rojo_case, LAWYER_A_RUT, "Lawyer A")
        _seed_abogado(db, verde_case, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        rols = [c["rol"] for c in result["top_critical"]]
        assert rols.index("C-9301-2025") < rols.index("C-9302-2025")

    def test_soonest_deadline_ranks_first_within_same_semaforo(self, db, firm_account, court):
        today = date.today()
        soon = _make_case(
            db, firm_account, court, "C-9303-2025", semaforo="rojo",
            next_deadline_at=today + timedelta(days=1),
        )
        later = _make_case(
            db, firm_account, court, "C-9304-2025", semaforo="rojo",
            next_deadline_at=today + timedelta(days=20),
        )
        _seed_abogado(db, soon, LAWYER_A_RUT, "Lawyer A")
        _seed_abogado(db, later, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        rols = [c["rol"] for c in result["top_critical"]]
        assert rols.index("C-9303-2025") < rols.index("C-9304-2025")

    def test_fatal_ranks_before_non_fatal_on_tie(self, db, firm_account, court):
        today = date.today()
        same_date = today + timedelta(days=5)
        fatal = _make_case(
            db, firm_account, court, "C-9305-2025", semaforo="rojo",
            next_deadline_at=same_date, next_deadline_fatal=True,
        )
        non_fatal = _make_case(
            db, firm_account, court, "C-9306-2025", semaforo="rojo",
            next_deadline_at=same_date, next_deadline_fatal=False,
        )
        _seed_abogado(db, fatal, LAWYER_A_RUT, "Lawyer A")
        _seed_abogado(db, non_fatal, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        rols = [c["rol"] for c in result["top_critical"]]
        assert rols.index("C-9305-2025") < rols.index("C-9306-2025")

    def test_null_next_deadline_at_does_not_crash_and_ranks_last(self, db, firm_account, court):
        with_deadline = _make_case(
            db, firm_account, court, "C-9307-2025", semaforo="rojo",
            next_deadline_at=date.today() + timedelta(days=2),
        )
        without_deadline = _make_case(
            db, firm_account, court, "C-9308-2025", semaforo="rojo",
            next_deadline_at=None,
        )
        _seed_abogado(db, with_deadline, LAWYER_A_RUT, "Lawyer A")
        _seed_abogado(db, without_deadline, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        rols = [c["rol"] for c in result["top_critical"]]
        assert rols.index("C-9307-2025") < rols.index("C-9308-2025")

    def test_top_critical_capped_at_15(self, db, firm_account, court):
        for i in range(20):
            c = _make_case(
                db, firm_account, court, f"C-94{i:02d}-2025", semaforo="rojo",
                next_deadline_at=date.today() + timedelta(days=i),
            )
            _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        assert len(result["top_critical"]) <= 15

    def test_top_critical_shape(self, db, firm_account, court):
        c = _make_case(
            db, firm_account, court, "C-9401-2025", semaforo="rojo",
            next_deadline_at=date.today() + timedelta(days=2),
            next_deadline_fatal=True,
            plaintiff="Banco X", defendant="Juan Pérez",
        )
        _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        row = result["top_critical"][0]
        for key in (
            "case_id", "rol", "caratulado", "tribunal", "abogado_nombre",
            "semaforo", "next_deadline_at", "next_deadline_fatal",
        ):
            assert key in row, f"missing key {key}"
        assert row["rol"] == "C-9401-2025"
        assert row["caratulado"] == "Banco X/Juan Pérez"
        assert row["tribunal"] == "1er Juzgado Civil de Santiago"
        assert row["abogado_nombre"] == "Lawyer A"


# ---------------------------------------------------------------------------
# Unit tests: firm_risk_board — archived exclusion
# ---------------------------------------------------------------------------


class TestFirmRiskBoardArchived:
    def test_archived_cases_excluded(self, db, firm_account, court):
        active = _make_case(db, firm_account, court, "C-9501-2025", semaforo="rojo", status="active")
        archived = _make_case(db, firm_account, court, "C-9502-2025", semaforo="rojo", status="archived")
        _seed_abogado(db, active, LAWYER_A_RUT, "Lawyer A")
        _seed_abogado(db, archived, LAWYER_A_RUT, "Lawyer A")
        result = firm_risk_board(db, ACCOUNT_RUT)
        assert result["total"] == 1
        assert result["semaforo"]["rojo"] == 1
        rols = [c["rol"] for c in result["top_critical"]]
        assert "C-9502-2025" not in rols


# ---------------------------------------------------------------------------
# Endpoint tests: GET /api/v1/stats/risk-board
# ---------------------------------------------------------------------------


@pytest.fixture
def auditor(db):
    l = Lawyer(rut=AUDITOR_RUT, name="Auditor User", role="auditor")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def admin_lawyer(db):
    l = Lawyer(rut=ADMIN_RUT, name="Admin User", role="admin")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def regular_lawyer(db):
    l = Lawyer(rut=REGULAR_LAWYER_RUT, name="Regular Lawyer", role="lawyer")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def auditor_headers(auditor):
    tok = create_access_token({"sub": AUDITOR_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def admin_headers(admin_lawyer):
    tok = create_access_token({"sub": ADMIN_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def regular_headers(regular_lawyer):
    tok = create_access_token({"sub": REGULAR_LAWYER_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


class TestRiskBoardEndpoint:
    def test_endpoint_200_for_auditor(self, client, db, auditor, auditor_headers, court):
        c = _make_case(db, auditor, court, "C-9601-2025", semaforo="rojo")
        _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        resp = client.get("/api/v1/stats/risk-board", headers=auditor_headers)
        assert resp.status_code == 200

    def test_endpoint_200_for_admin(self, client, db, admin_lawyer, admin_headers, court):
        c = _make_case(db, admin_lawyer, court, "C-9602-2025", semaforo="rojo")
        _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        resp = client.get("/api/v1/stats/risk-board", headers=admin_headers)
        assert resp.status_code == 200

    def test_endpoint_403_for_regular_lawyer(self, client, regular_lawyer, regular_headers):
        resp = client.get("/api/v1/stats/risk-board", headers=regular_headers)
        assert resp.status_code == 403

    def test_endpoint_response_matches_model(self, client, db, auditor, auditor_headers, court):
        c = _make_case(
            db, auditor, court, "C-9603-2025", semaforo="rojo",
            abandono_disponible=True, next_deadline_fatal=True,
            next_deadline_at=date.today() + timedelta(days=1),
        )
        _seed_abogado(db, c, LAWYER_A_RUT, "Lawyer A")
        resp = client.get("/api/v1/stats/risk-board", headers=auditor_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"total", "semaforo", "riesgo", "by_lawyer", "top_critical"}
        assert set(data["semaforo"].keys()) == {"rojo", "amarillo", "verde", "gris"}
        assert set(data["riesgo"].keys()) == {
            "abandono_disponible", "prescripcion_cumplida", "en_apremio",
            "plazo_fatal_proximo", "plazo_fatal_vencido",
        }
        assert data["total"] == 1
        row = data["by_lawyer"][0]
        assert set(row.keys()) == {"rut", "nombre", "rojo", "abandono_disponible", "en_apremio", "total"}
        crit = data["top_critical"][0]
        assert set(crit.keys()) == {
            "case_id", "rol", "caratulado", "tribunal", "abogado_nombre",
            "semaforo", "next_deadline_at", "next_deadline_fatal",
        }
