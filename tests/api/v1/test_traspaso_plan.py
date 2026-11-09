"""Traspaso de cartera — planning service (app/services/traspaso_plan.py) and
POST /asignacion/traspaso/plan. The plan is a dry run: it never writes."""
from datetime import date, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_assignment_history import CaseAssignmentHistory
from app.models.case_deadline import CaseDeadline
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.liberacion import LiberacionRequest
from app.services.traspaso_plan import PlanError, build_plan

ADMIN_RUT = "11111111-1"
PLAN_URL = "/api/v1/asignacion/traspaso/plan"


def _lawyer(db, rut, name, nivel=None, **kw):
    obj = Lawyer(rut=rut, name=name, role="lawyer", is_firm_lawyer=True, nivel=nivel, **kw)
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Admin", role="admin", is_firm_lawyer=False)
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-TRASP", name="Juzgado Traspaso", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def origen(db):
    return _lawyer(db, "10000001-1", "Origen", "pleno")


@pytest.fixture
def dest_a(db):
    return _lawyer(db, "10000002-2", "Dest A", "pleno")


@pytest.fixture
def dest_b(db):
    return _lawyer(db, "10000003-3", "Dest B", "pleno")


def _case(db, owner, court, rol, *, assigned=None, matriz="M2", origen_matriz="pjud_etapa", status="active", **kw):
    obj = Case(
        lawyer_id=owner.id, court_id=court.id, rol=rol, status=status, competencia="civil",
        matriz=matriz, matriz_origen=origen_matriz, assigned_lawyer_id=assigned.id if assigned else None,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(), **kw,
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _by_case(plan):
    return {p["case_id"]: p for p in plan["propuesta"]}


# --- ownership selection ---------------------------------------------------


def test_selects_cases_owned_by_override(db, origen, dest_a, court):
    mine = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    _case(db, dest_a, court, "C-2-2026", assigned=dest_a)
    plan = build_plan(db, origen.id)
    assert [p["case_id"] for p in plan["propuesta"]] == [mine.id]
    assert plan["resumen"]["total"] == 1


def test_selects_cases_owned_by_litigante_and_override_replaces_it(db, origen, dest_a, court):
    by_lit = _case(db, dest_a, court, "C-1-2026")
    db.add(CaseLitigante(case_id=by_lit.id, participante="AB.DDO", rut=origen.rut,
                         persona_type="NATURAL", nombre="Origen", natural_key="k1"))
    moved = _case(db, dest_a, court, "C-2-2026", assigned=dest_a)
    db.add(CaseLitigante(case_id=moved.id, participante="AB.DDO", rut=origen.rut,
                         persona_type="NATURAL", nombre="Origen", natural_key="k2"))
    db.commit()
    plan = build_plan(db, origen.id)
    assert [p["case_id"] for p in plan["propuesta"]] == [by_lit.id]


def test_archived_cases_are_excluded(db, origen, dest_a, court):
    _case(db, dest_a, court, "C-1-2026", assigned=origen, status="archived")
    plan = build_plan(db, origen.id)
    assert plan["propuesta"] == []
    assert plan["resumen"]["total"] == 0


# --- summary ---------------------------------------------------------------


def test_summary_counts(db, origen, dest_a, court):
    c1 = _case(db, dest_a, court, "C-1-2026", assigned=origen, matriz="M3", semaforo="rojo")
    _case(db, dest_a, court, "C-2-2026", assigned=origen, matriz="M2", semaforo="verde")
    c3 = _case(db, dest_a, court, "C-3-2026", assigned=origen, matriz="M2", semaforo="rojo")
    today = date.today()
    db.add_all([
        CaseDeadline(case_id=c1.id, deadline_type="a", due_date=today + timedelta(days=3),
                     triggered_at=today, status="active"),
        CaseDeadline(case_id=c3.id, deadline_type="a", due_date=today + timedelta(days=11),
                     triggered_at=today, status="active"),
        CaseDeadline(case_id=c3.id, deadline_type="b", due_date=today + timedelta(days=2),
                     triggered_at=today, status="met"),
        LiberacionRequest(case_id=c1.id, target_semaforo="verde", estado="pendiente"),
        LiberacionRequest(case_id=c3.id, target_semaforo="verde", estado="aplicado"),
    ])
    db.commit()
    r = build_plan(db, origen.id)["resumen"]
    assert r["total"] == 3
    assert r["por_nivel"] == {"M3": 1, "M2": 2}
    assert r["por_semaforo"] == {"rojo": 2, "verde": 1}
    assert r["plazos_proximos"] == 1
    assert r["liberaciones_pendientes"] == 1


def test_solo_cuenta_origen(db, origen, dest_a, court):
    only = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    shared = _case(db, dest_a, court, "C-2-2026", assigned=origen)
    nosight = _case(db, origen, court, "C-3-2026", assigned=origen)  # no rows, lawyer_id == origen
    unknown = _case(db, dest_a, court, "C-4-2026", assigned=origen)  # no rows, lawyer_id != origen
    db.add_all([
        CaseLawyerSource(case_id=only.id, lawyer_id=origen.id),
        CaseLawyerSource(case_id=shared.id, lawyer_id=origen.id),
        CaseLawyerSource(case_id=shared.id, lawyer_id=dest_a.id),
    ])
    db.commit()
    plan = build_plan(db, origen.id)
    got = _by_case(plan)
    assert got[only.id]["solo_cuenta_origen"] is True
    assert got[shared.id]["solo_cuenta_origen"] is False
    assert got[nosight.id]["solo_cuenta_origen"] is True
    assert got[unknown.id]["solo_cuenta_origen"] is False
    assert plan["resumen"]["solo_cuenta_origen"] == 2


# --- distribution ----------------------------------------------------------


def test_urgent_first_and_spread(db, origen, dest_a, dest_b, court):
    today = date.today()
    rojo1 = _case(db, dest_a, court, "C-1-2026", assigned=origen, semaforo="rojo", next_deadline_at=today + timedelta(days=5))
    rojo2 = _case(db, dest_a, court, "C-2-2026", assigned=origen, semaforo="rojo", next_deadline_at=today + timedelta(days=1))
    amar = _case(db, dest_a, court, "C-3-2026", assigned=origen, semaforo="amarillo")
    plan = build_plan(db, origen.id)
    assert [p["case_id"] for p in plan["propuesta"]] == [rojo2.id, rojo1.id, amar.id]
    got = _by_case(plan)
    assert got[rojo2.id]["destino_lawyer_id"] == dest_a.id
    assert got[rojo1.id]["destino_lawyer_id"] == dest_b.id
    assert got[amar.id]["destino_lawyer_id"] == dest_a.id
    carga = {c["lawyer_id"]: c for c in plan["carga"]}
    assert carga[dest_a.id]["recibe"] == 2
    assert carga[dest_a.id]["recibe_urgentes"] == 1
    assert carga[dest_b.id]["recibe_urgentes"] == 1
    assert carga[dest_a.id]["carga_final"] == carga[dest_a.id]["carga_actual"] + 2


def test_item_shape_and_proximo_plazo(db, origen, dest_a, court):
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen, semaforo="rojo")
    today = date.today()
    db.add_all([
        CaseDeadline(case_id=c.id, deadline_type="a", due_date=today + timedelta(days=9), triggered_at=today, status="active"),
        CaseDeadline(case_id=c.id, deadline_type="b", due_date=today + timedelta(days=4), triggered_at=today, status="active"),
    ])
    db.commit()
    item = build_plan(db, origen.id)["propuesta"][0]
    assert set(item) == {"case_id", "rol", "tribunal", "matriz", "semaforo", "proximo_plazo",
                         "destino_lawyer_id", "destino_nombre", "advertencia", "solo_cuenta_origen"}
    assert item["rol"] == "C-1-2026"
    assert item["tribunal"] == "Juzgado Traspaso"
    assert item["proximo_plazo"] == today + timedelta(days=4)
    assert item["destino_nombre"] == "Dest A"
    assert item["advertencia"] is None


def test_level_match_and_warning(db, origen, court):
    junior = _lawyer(db, "10000010-0", "Junior", "junior")
    senior = _lawyer(db, "10000011-1", "Senior", "senior")
    baja = _case(db, junior, court, "C-1-2026", assigned=origen, matriz="M1 Baja")
    m3 = _case(db, junior, court, "C-2-2026", assigned=origen, matriz="M3")
    got = _by_case(build_plan(db, origen.id))
    assert got[baja.id]["destino_lawyer_id"] == junior.id and got[baja.id]["advertencia"] is None
    assert got[m3.id]["destino_lawyer_id"] == senior.id and got[m3.id]["advertencia"] is None


def test_no_level_match_warns_and_uses_min_load(db, origen, court):
    junior = _lawyer(db, "10000010-0", "Junior", "junior")
    pleno = _lawyer(db, "10000012-2", "Pleno", "pleno")
    _case(db, pleno, court, "C-0-2026", assigned=pleno)  # pleno carries 1 already
    m3 = _case(db, junior, court, "C-1-2026", assigned=origen, matriz="M3")
    item = _by_case(build_plan(db, origen.id))[m3.id]
    assert item["advertencia"] == "nivel no coincide"
    assert item["destino_lawyer_id"] == junior.id


def test_sin_matriz_and_provisional_warn(db, origen, dest_a, court):
    nomat = _case(db, dest_a, court, "C-1-2026", assigned=origen, matriz=None)
    prov = _case(db, dest_a, court, "C-2-2026", assigned=origen, matriz="M2", origen_matriz="sin_detalle")
    got = _by_case(build_plan(db, origen.id))
    assert got[nomat.id]["advertencia"] == "sin matriz"
    assert got[prov.id]["advertencia"] == "sin matriz"


def test_ties_go_to_lowest_lawyer_id(db, origen, dest_a, dest_b, court):
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    assert _by_case(build_plan(db, origen.id))[c.id]["destino_lawyer_id"] == min(dest_a.id, dest_b.id)


def test_existing_load_is_considered(db, origen, dest_a, dest_b, court):
    _case(db, dest_a, court, "C-0-2026", assigned=dest_a)
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    assert _by_case(build_plan(db, origen.id))[c.id]["destino_lawyer_id"] == dest_b.id


def test_destino_filter_and_destino_unico(db, origen, dest_a, dest_b, court):
    c1 = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    c2 = _case(db, dest_a, court, "C-2-2026", assigned=origen, matriz="M3")
    filtered = build_plan(db, origen.id, destino_ids=[dest_b.id])
    assert {p["destino_lawyer_id"] for p in filtered["propuesta"]} == {dest_b.id}
    assert [c["lawyer_id"] for c in filtered["carga"]] == [dest_b.id]
    unico = _by_case(build_plan(db, origen.id, destino_unico_id=dest_b.id))
    assert unico[c1.id]["destino_lawyer_id"] == dest_b.id and unico[c1.id]["advertencia"] is None
    assert unico[c2.id]["destino_lawyer_id"] == dest_b.id and unico[c2.id]["advertencia"] == "nivel no coincide"


def test_inactive_and_non_firm_are_not_eligible(db, origen, dest_a, court):
    _lawyer(db, "10000020-0", "Blocked", "pleno", is_active=False)
    ext = _lawyer(db, "10000021-1", "Ext", "pleno")
    ext.is_firm_lawyer = False
    db.commit()
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    plan = build_plan(db, origen.id)
    assert {x["lawyer_id"] for x in plan["carga"]} == {dest_a.id}
    assert _by_case(plan)[c.id]["destino_lawyer_id"] == dest_a.id


def test_plan_errors(db, origen, dest_a):
    with pytest.raises(PlanError):
        build_plan(db, 9999)
    with pytest.raises(PlanError):
        build_plan(db, origen.id, destino_ids=[origen.id])
    blocked = _lawyer(db, "10000030-0", "Blocked", "pleno", is_active=False)
    with pytest.raises(PlanError):
        build_plan(db, origen.id, destino_ids=[blocked.id])
    with pytest.raises(PlanError):
        build_plan(db, origen.id, destino_unico_id=blocked.id)
    with pytest.raises(PlanError):
        build_plan(db, origen.id, destino_unico_id=origen.id)


def test_no_eligible_destinations(db, origen):
    with pytest.raises(PlanError):
        build_plan(db, origen.id)


# --- endpoint --------------------------------------------------------------


def _headers(rut):
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


def test_endpoint_plan_writes_nothing(client, db, admin, origen, dest_a, court):
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    r = client.post(PLAN_URL, json={"origen_lawyer_id": origen.id}, headers=_headers(ADMIN_RUT))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resumen"]["total"] == 1 and body["propuesta"][0]["destino_lawyer_id"] == dest_a.id
    db.expire_all()
    assert db.get(Case, c.id).assigned_lawyer_id == origen.id
    assert db.query(CaseAssignmentHistory).count() == 0


def test_endpoint_plan_422s(client, db, admin, origen):
    h = _headers(ADMIN_RUT)
    assert client.post(PLAN_URL, json={"origen_lawyer_id": 9999}, headers=h).status_code == 422
    assert client.post(PLAN_URL, json={"origen_lawyer_id": origen.id}, headers=h).status_code == 422


@pytest.mark.parametrize("rut,role", [("20000001-1", "lawyer"), ("20000002-2", "auditor")])
def test_endpoint_plan_admin_only(client, db, origen, rut, role):
    db.add(Lawyer(rut=rut, name="X", role=role)); db.commit()
    r = client.post(PLAN_URL, json={"origen_lawyer_id": origen.id}, headers=_headers(rut))
    assert r.status_code == 403
