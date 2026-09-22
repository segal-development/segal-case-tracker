"""Tests for the matriz de clasificación endpoints (app/api/v1/matriz.py)
and the ``?matriz=`` filter on GET /api/v1/cases."""
from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.matriz_pjud_mapeo import MatrizPjudMapeo

ADMIN_RUT = "11111111-1"
AUDITOR_RUT = "77777777-7"
LAWYER_A_RUT = "88888888-8"
LAWYER_B_RUT = "99999999-9"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Admin", role="admin")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def auditor(db):
    obj = Lawyer(rut=AUDITOR_RUT, name="Auditor", role="auditor")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def lawyer_a(db):
    obj = Lawyer(rut=LAWYER_A_RUT, name="Lawyer A", role="lawyer", is_firm_lawyer=True)
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def lawyer_b(db):
    obj = Lawyer(rut=LAWYER_B_RUT, name="Lawyer B", role="lawyer", is_firm_lawyer=True)
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-MTZAPI", name="Juzgado Matriz API", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _headers(rut):
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def admin_headers(admin):
    return _headers(ADMIN_RUT)


@pytest.fixture
def auditor_headers(auditor):
    return _headers(AUDITOR_RUT)


@pytest.fixture
def lawyer_a_headers(lawyer_a):
    return _headers(LAWYER_A_RUT)


def _make_case(db, owner_lawyer, court, rol, *, matriz=None, matriz_origen=None, **kwargs):
    obj = Case(
        lawyer_id=owner_lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        matriz=matriz,
        matriz_origen=matriz_origen,
        matriz_computed_at=datetime.utcnow() if matriz_origen else None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        **kwargs,
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _seed_abogado_litigante(db, case, rut, nombre):
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
def dataset(db, admin, lawyer_a, lawyer_b, court):
    """4 civil cases: 2 owned/litigated by lawyer_a (M1 Baja, M2), 1 by
    lawyer_b (M3), 1 unmapped (no_mapeada, matriz None)."""
    case_1 = _make_case(db, lawyer_a, court, "C-1-2026", matriz="M1 Baja", matriz_origen="pjud_etapa")
    case_2 = _make_case(db, lawyer_a, court, "C-2-2026", matriz="M2", matriz_origen="pjud_etapa")
    case_3 = _make_case(db, lawyer_b, court, "C-3-2026", matriz="M3", matriz_origen="pjud_procedimiento")
    case_4 = _make_case(db, lawyer_b, court, "C-4-2026", matriz=None, matriz_origen="no_mapeada")

    _seed_abogado_litigante(db, case_1, LAWYER_A_RUT, "Lawyer A")
    _seed_abogado_litigante(db, case_2, LAWYER_A_RUT, "Lawyer A")
    _seed_abogado_litigante(db, case_3, LAWYER_B_RUT, "Lawyer B")
    _seed_abogado_litigante(db, case_4, LAWYER_B_RUT, "Lawyer B")

    return {"case_1": case_1, "case_2": case_2, "case_3": case_3, "case_4": case_4}


# ---------------------------------------------------------------------------
# GET /matriz/distribucion
# ---------------------------------------------------------------------------


class TestDistribucion:
    def test_admin_sees_firm_wide_distribution(self, client, admin_headers, dataset):
        resp = client.get("/api/v1/matriz/distribucion", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 4

        # Numbers must add up to the total.
        assert sum(item["causas"] for item in data["por_matriz"]) == 4
        assert sum(item["causas"] for item in data["por_origen"]) == 4

        by_matriz = {item["matriz"]: item["causas"] for item in data["por_matriz"]}
        assert by_matriz["M1 Baja"] == 1
        assert by_matriz["M2"] == 1
        assert by_matriz["M3"] == 1
        assert by_matriz[None] == 1

    def test_auditor_sees_firm_wide_distribution(self, client, auditor_headers, dataset):
        resp = client.get("/api/v1/matriz/distribucion", headers=auditor_headers)
        assert resp.status_code == 200
        assert resp.json()["total"] == 4

    def test_lawyer_sees_only_own_scope(self, client, lawyer_a_headers, dataset):
        resp = client.get("/api/v1/matriz/distribucion", headers=lawyer_a_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2  # only case_1 and case_2
        by_matriz = {item["matriz"]: item["causas"] for item in data["por_matriz"]}
        assert by_matriz.get("M3") is None
        assert by_matriz["M1 Baja"] == 1
        assert by_matriz["M2"] == 1

    def test_sin_mapear_reports_last_stage(self, db, client, admin_headers, dataset, court):
        from app.models.movement import Movement

        case_4 = dataset["case_4"]
        db.add(Movement(
            case_id=case_4.id,
            stage="Etapa Sin Mapeo",
            description="mov",
            movement_date=datetime(2026, 1, 1),
        ))
        db.commit()

        resp = client.get("/api/v1/matriz/distribucion", headers=admin_headers)
        sin_mapear = resp.json()["sin_mapear"]
        assert {"stage": "Etapa Sin Mapeo", "descripcion": None, "causas": 1} in sin_mapear

    def test_sin_mapear_groups_blank_stage_by_description(self, db, client, admin_headers, dataset):
        from app.models.movement import Movement

        case_4 = dataset["case_4"]
        db.add(Movement(
            case_id=case_4.id,
            stage=None,
            description="Un evento inédito sin mapear",
            movement_date=datetime(2026, 1, 1),
        ))
        db.commit()

        resp = client.get("/api/v1/matriz/distribucion", headers=admin_headers)
        sin_mapear = resp.json()["sin_mapear"]
        assert {"stage": None, "descripcion": "Un evento inédito sin mapear", "causas": 1} in sin_mapear

    def test_requires_auth(self, client):
        assert client.get("/api/v1/matriz/distribucion").status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /matriz/por-abogado
# ---------------------------------------------------------------------------


class TestPorAbogado:
    def test_admin_gets_per_lawyer_breakdown(self, client, admin_headers, dataset):
        resp = client.get("/api/v1/matriz/por-abogado", headers=admin_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json()["items"], list)

    def test_totals_match_dataset(self, client, admin_headers, dataset, lawyer_a, lawyer_b):
        resp = client.get("/api/v1/matriz/por-abogado", headers=admin_headers)
        items = {item["lawyer_id"]: item for item in resp.json()["items"]}
        assert items[lawyer_a.id]["total"] == 2
        assert items[lawyer_b.id]["total"] == 2

    def test_lawyer_role_forbidden(self, client, lawyer_a_headers):
        resp = client.get("/api/v1/matriz/por-abogado", headers=lawyer_a_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET/PUT /matriz/mapeo
# ---------------------------------------------------------------------------


class TestMapeoEditor:
    def test_admin_lists_mapeo(self, db, client, admin_headers):
        db.add(MatrizPjudMapeo(pjud_stage="Ingreso", matriz_etapa="ASIGNACIÓN", activo=True))
        db.commit()

        resp = client.get("/api/v1/matriz/mapeo", headers=admin_headers)
        assert resp.status_code == 200
        stages = {row["pjud_stage"] for row in resp.json()}
        assert "Ingreso" in stages

    def test_auditor_cannot_list_mapeo(self, client, auditor_headers):
        resp = client.get("/api/v1/matriz/mapeo", headers=auditor_headers)
        assert resp.status_code == 403

    def test_admin_can_update_mapeo(self, db, client, admin_headers):
        db.add(MatrizPjudMapeo(pjud_stage="Ingreso", matriz_etapa="ASIGNACIÓN", activo=True))
        db.commit()

        resp = client.put(
            "/api/v1/matriz/mapeo/Ingreso",
            headers=admin_headers,
            json={"matriz_etapa": "DEMANDA NOTIFICADA", "activo": False},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["matriz_etapa"] == "DEMANDA NOTIFICADA"
        assert body["activo"] is False

    def test_update_unknown_stage_is_404(self, client, admin_headers):
        resp = client.put(
            "/api/v1/matriz/mapeo/No Existe",
            headers=admin_headers,
            json={"activo": False},
        )
        assert resp.status_code == 404

    def test_match_tipo_disambiguates_same_text(self, db, client, admin_headers):
        """'Sentencia' exists as BOTH a stage rule and a descripcion rule —
        the match_tipo query param must edit exactly one of them."""
        from app.models.matriz_pjud_mapeo import MATCH_TIPO_DESCRIPCION, MATCH_TIPO_STAGE

        db.add(MatrizPjudMapeo(
            pjud_stage="Sentencia", matriz_etapa="FASE DECLARATIVA", match_tipo=MATCH_TIPO_STAGE,
        ))
        db.add(MatrizPjudMapeo(
            pjud_stage="Sentencia", matriz_etapa="FASE DECLARATIVA",
            match_tipo=MATCH_TIPO_DESCRIPCION, orden=3,
        ))
        db.commit()

        resp = client.put(
            "/api/v1/matriz/mapeo/Sentencia?match_tipo=descripcion",
            headers=admin_headers,
            json={"activo": False, "orden": 9},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["match_tipo"] == "descripcion"
        assert body["activo"] is False
        assert body["orden"] == 9

        # The stage-type row must be untouched.
        stage_row = (
            db.query(MatrizPjudMapeo)
            .filter_by(pjud_stage="Sentencia", match_tipo=MATCH_TIPO_STAGE)
            .first()
        )
        assert stage_row.activo is True

    def test_list_mapeo_includes_match_tipo_and_orden(self, db, client, admin_headers):
        from app.models.matriz_pjud_mapeo import MATCH_TIPO_DESCRIPCION

        db.add(MatrizPjudMapeo(
            pjud_stage="Cita a Audiencia", matriz_etapa="FASE DECLARATIVA",
            match_tipo=MATCH_TIPO_DESCRIPCION, orden=2,
        ))
        db.commit()

        resp = client.get("/api/v1/matriz/mapeo", headers=admin_headers)
        row = next(r for r in resp.json() if r["pjud_stage"] == "Cita a Audiencia")
        assert row["match_tipo"] == "descripcion"
        assert row["orden"] == 2


# ---------------------------------------------------------------------------
# GET /cases?matriz=
# ---------------------------------------------------------------------------


class TestCasesMatrizFilter:
    def test_filter_returns_only_matching(self, client, admin_headers, dataset):
        resp = client.get("/api/v1/cases?matriz=M2", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == dataset["case_2"].id
        assert data["items"][0]["matriz"] == "M2"
        assert data["items"][0]["matriz_origen"] == "pjud_etapa"

    def test_no_filter_returns_all(self, client, admin_headers, dataset):
        resp = client.get("/api/v1/cases", headers=admin_headers)
        assert resp.json()["total"] == 4

    def test_detail_endpoint_includes_matriz_fields(self, client, admin_headers, dataset):
        resp = client.get(f"/api/v1/cases/{dataset['case_3'].id}", headers=admin_headers)
        assert resp.status_code == 200
        case = resp.json()["case"]
        assert case["matriz"] == "M3"
        assert case["matriz_origen"] == "pjud_procedimiento"
