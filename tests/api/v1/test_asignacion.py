"""Tests for asignación por nivel (app/api/v1/asignacion.py) and the
Case.assigned_lawyer_id / effective_lawyer_id split (app/models/case.py).

Covers: effective-lawyer property/SQL-helper agreement, the sync/provenance
invariant the whole feature exists to protect, desajustes level rules and
solo_firmes, reasignar audit fields + wrong-level warnings, DELETE clearing
the override, role guards, the `/cases?asignado=` filter, and that a
lawyer's own case list follows a reassignment.
"""
from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.case import EFFECTIVE_LAWYER_ID, Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer

ADMIN_RUT = "11111111-1"
AUDITOR_RUT = "77777777-7"
JUNIOR_RUT = "22222222-2"
PLENO_RUT = "33333333-3"
SENIOR_RUT = "44444444-4"
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
def junior(db):
    obj = Lawyer(rut=JUNIOR_RUT, name="Junior Uno", role="lawyer", is_firm_lawyer=True, nivel="junior")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def pleno(db):
    obj = Lawyer(rut=PLENO_RUT, name="Pleno Uno", role="lawyer", is_firm_lawyer=True, nivel="pleno")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def senior(db):
    # "senior" is not settable via PUT /bono/nivel today (see final report),
    # but the DB column itself is a plain nullable String(10) — writing it
    # directly is exactly what the matching logic in asignacion.py expects.
    obj = Lawyer(rut=SENIOR_RUT, name="Senior Uno", role="lawyer", is_firm_lawyer=True, nivel="senior")
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
    obj = Court(code="T1-ASIGAPI", name="Juzgado Asignación API", region="RM", type="civil")
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


@pytest.fixture
def lawyer_b_headers(lawyer_b):
    return _headers(LAWYER_B_RUT)


def _make_case(db, owner_lawyer, court, rol, *, matriz=None, matriz_origen="pjud_etapa", **kwargs):
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


# ---------------------------------------------------------------------------
# effective_lawyer_id property <-> EFFECTIVE_LAWYER_ID SQL helper
# ---------------------------------------------------------------------------


def test_effective_lawyer_id_property_matches_sql_helper(db, lawyer_a, lawyer_b, court):
    not_reassigned = _make_case(db, lawyer_a, court, "C-1-2026")
    reassigned = _make_case(db, lawyer_a, court, "C-2-2026")
    reassigned.assigned_lawyer_id = lawyer_b.id
    db.commit()

    assert not_reassigned.effective_lawyer_id == lawyer_a.id
    assert reassigned.effective_lawyer_id == lawyer_b.id

    sql_results = dict(
        db.query(Case.id, EFFECTIVE_LAWYER_ID).filter(
            Case.id.in_([not_reassigned.id, reassigned.id])
        ).all()
    )
    assert sql_results[not_reassigned.id] == not_reassigned.effective_lawyer_id
    assert sql_results[reassigned.id] == reassigned.effective_lawyer_id


# ---------------------------------------------------------------------------
# THE invariant: reassignment must never break sync dedup (existing_by_rol)
# ---------------------------------------------------------------------------


def test_reassignment_does_not_break_sync_dedup(db, lawyer_a, lawyer_b, court):
    """A causa reassigned to another internal lawyer must still be found (and
    updated, not re-created) by the next sync of the ORIGINAL lawyer's PJUD
    account — existing_by_rol must key on lawyer_id, never on the
    asignación-por-nivel override."""
    from app.services.sync_service import ScrapedCase, SyncService

    case = _make_case(db, lawyer_a, court, "C-500-2024", matriz="M1 Baja")
    case.assigned_lawyer_id = lawyer_b.id
    case.assigned_at = datetime.utcnow()
    case.assigned_by_rut = ADMIN_RUT
    db.commit()

    scraped = [
        ScrapedCase(
            rol="C-500-2024",
            tribunal=court.name,
            caratulado="Banco/Deudor",
            fecha_ingreso="01/01/2024",
            estado_cuaderno="En tramitación",
            cuaderno="Ejecutivo",
        )
    ]

    # Exercises the exact existing_by_rol lookup used by SyncService.sync_cases:
    # it must find the causa under the ORIGINAL syncing lawyer (lawyer_a), not
    # under the reassigned effective lawyer (lawyer_b).
    existing_by_rol = {
        c.rol: c for c in db.query(Case).filter(Case.lawyer_id == lawyer_a.id).all()
    }
    assert "C-500-2024" in existing_by_rol

    result = SyncService(db).sync_cases(lawyer_a.id, scraped, competencia="civil")

    assert result.cases_new == 0
    assert result.cases_updated == 1

    rows = db.query(Case).filter(Case.rol == "C-500-2024").all()
    assert len(rows) == 1  # no duplicate created
    assert rows[0].lawyer_id == lawyer_a.id  # provenance untouched
    assert rows[0].assigned_lawyer_id == lawyer_b.id  # assignment preserved


# ---------------------------------------------------------------------------
# GET /asignacion/desajustes
# ---------------------------------------------------------------------------


def test_desajustes_m2_with_junior_appears(client, db, admin_headers, junior, court):
    _make_case(db, junior, court, "C-10-2026", matriz="M2")
    resp = client.get("/api/v1/asignacion/desajustes", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert any(i["rol"] == "C-10-2026" for i in body["items"])


def test_desajustes_m3_with_pleno_appears(client, db, admin_headers, pleno, court):
    _make_case(db, pleno, court, "C-11-2026", matriz="M3")
    resp = client.get("/api/v1/asignacion/desajustes", headers=admin_headers)
    body = resp.json()
    assert any(i["rol"] == "C-11-2026" for i in body["items"])


def test_desajustes_m1_baja_with_junior_does_not_appear(client, db, admin_headers, junior, court):
    _make_case(db, junior, court, "C-12-2026", matriz="M1 Baja")
    resp = client.get("/api/v1/asignacion/desajustes", headers=admin_headers)
    body = resp.json()
    assert not any(i["rol"] == "C-12-2026" for i in body["items"])


def test_desajustes_m1_alta_with_senior_does_not_appear(client, db, admin_headers, senior, court):
    _make_case(db, senior, court, "C-13-2026", matriz="M1 Alta")
    resp = client.get("/api/v1/asignacion/desajustes", headers=admin_headers)
    body = resp.json()
    assert not any(i["rol"] == "C-13-2026" for i in body["items"])


def test_desajustes_solo_firmes_default_excludes_sin_detalle(client, db, admin_headers, senior, court):
    """A sin_detalle M1 Baja held by a senior is provisional, not evidence of
    a real desajuste — must NOT appear with solo_firmes default (true)."""
    _make_case(db, senior, court, "C-14-2026", matriz="M1 Baja", matriz_origen="sin_detalle")

    resp = client.get("/api/v1/asignacion/desajustes", headers=admin_headers)
    body = resp.json()
    assert not any(i["rol"] == "C-14-2026" for i in body["items"])

    resp2 = client.get(
        "/api/v1/asignacion/desajustes", params={"solo_firmes": "false"}, headers=admin_headers
    )
    body2 = resp2.json()
    assert any(i["rol"] == "C-14-2026" for i in body2["items"])


def test_desajustes_response_shape(client, db, admin_headers, junior, court):
    case = _make_case(db, junior, court, "C-15-2026", matriz="M2", plaintiff="Banco", defendant="Deudor")
    resp = client.get(
        "/api/v1/asignacion/desajustes", params={"matriz": "M2"}, headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    item = next(i for i in body["items"] if i["case_id"] == case.id)
    assert item["lawyer_actual"] == {"id": junior.id, "nombre": junior.name, "nivel": "junior"}
    assert item["niveles_requeridos"] == ["pleno", "senior"]
    assert item["asignado"] is False
    assert item["tribunal"] == court.name
    assert body["total"] >= 1
    assert any(r["matriz"] == "M2" and r["nivel_actual"] == "junior" for r in body["resumen"])


# ---------------------------------------------------------------------------
# POST /asignacion/reasignar
# ---------------------------------------------------------------------------


def test_reasignar_sets_audit_fields(client, db, admin_headers, junior, pleno, court):
    case = _make_case(db, junior, court, "C-20-2026", matriz="M2")

    resp = client.post(
        "/api/v1/asignacion/reasignar",
        json={"case_ids": [case.id], "lawyer_id": pleno.id, "motivo": "Balanceo de carga"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"reasignadas": 1, "omitidas": [], "advertencias": []}

    db.refresh(case)
    assert case.assigned_lawyer_id == pleno.id
    assert case.assigned_at is not None
    assert case.assigned_by_rut == ADMIN_RUT
    assert case.assigned_motivo == "Balanceo de carga"
    assert case.lawyer_id == junior.id  # provenance untouched


def test_reasignar_wrong_level_warns_but_succeeds(client, db, admin_headers, junior, court):
    """Assigning a M3-exclusive causa to a junior is allowed, but warned."""
    case = _make_case(db, junior, court, "C-21-2026", matriz="M3")

    resp = client.post(
        "/api/v1/asignacion/reasignar",
        json={"case_ids": [case.id], "lawyer_id": junior.id},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reasignadas"] == 1
    assert len(body["advertencias"]) == 1
    assert "C-21-2026" in body["advertencias"][0]

    db.refresh(case)
    assert case.assigned_lawyer_id == junior.id  # not blocked


def test_reasignar_missing_case_is_omitted(client, db, admin_headers, pleno, court):
    case = _make_case(db, pleno, court, "C-22-2026", matriz="M2")
    missing_id = case.id + 9999

    resp = client.post(
        "/api/v1/asignacion/reasignar",
        json={"case_ids": [case.id, missing_id], "lawyer_id": pleno.id},
        headers=admin_headers,
    )
    body = resp.json()
    assert body["reasignadas"] == 1
    assert body["omitidas"] == [{"case_id": missing_id, "motivo": "Causa no encontrada"}]


def test_reasignar_inactive_target_rejected(client, db, admin_headers, pleno, court):
    pleno.is_active = False
    db.commit()
    case = _make_case(db, pleno, court, "C-23-2026", matriz="M2")

    resp = client.post(
        "/api/v1/asignacion/reasignar",
        json={"case_ids": [case.id], "lawyer_id": pleno.id},
        headers=admin_headers,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /asignacion/{case_id}
# ---------------------------------------------------------------------------


def test_delete_clears_override(client, db, admin_headers, junior, pleno, court):
    case = _make_case(db, junior, court, "C-30-2026", matriz="M2")
    case.assigned_lawyer_id = pleno.id
    case.assigned_at = datetime.utcnow()
    case.assigned_by_rut = ADMIN_RUT
    case.assigned_motivo = "test"
    db.commit()

    resp = client.delete(f"/api/v1/asignacion/{case.id}", headers=admin_headers)
    assert resp.status_code == 204

    db.refresh(case)
    assert case.assigned_lawyer_id is None
    assert case.assigned_at is None
    assert case.assigned_by_rut is None
    assert case.assigned_motivo is None
    assert case.lawyer_id == junior.id


def test_delete_missing_case_404(client, admin_headers):
    resp = client.delete("/api/v1/asignacion/999999", headers=admin_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /asignacion/sugerencias
# ---------------------------------------------------------------------------


def test_sugerencias_groups_by_nivel_with_counts(client, db, auditor_headers, junior, pleno, senior, court):
    _make_case(db, pleno, court, "C-40-2026", matriz="M2")

    resp = client.get("/api/v1/asignacion/sugerencias", headers=auditor_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["por_nivel"].keys()) == {"junior", "pleno", "senior"}

    pleno_row = next(r for r in body["por_nivel"]["pleno"] if r["lawyer_id"] == pleno.id)
    assert pleno_row["causas_actuales"] == 1

    junior_row = next(r for r in body["por_nivel"]["junior"] if r["lawyer_id"] == junior.id)
    assert junior_row["causas_actuales"] == 0


# ---------------------------------------------------------------------------
# Role guards
# ---------------------------------------------------------------------------


def test_role_guards_reject_plain_lawyer(client, db, lawyer_a_headers, lawyer_a, court):
    case = _make_case(db, lawyer_a, court, "C-50-2026", matriz="M2")

    assert client.get("/api/v1/asignacion/desajustes", headers=lawyer_a_headers).status_code == 403
    assert client.get("/api/v1/asignacion/sugerencias", headers=lawyer_a_headers).status_code == 403
    assert client.post(
        "/api/v1/asignacion/reasignar",
        json={"case_ids": [case.id], "lawyer_id": lawyer_a.id},
        headers=lawyer_a_headers,
    ).status_code == 403
    assert client.delete(f"/api/v1/asignacion/{case.id}", headers=lawyer_a_headers).status_code == 403


def test_auditor_can_read_but_not_reassign(client, db, auditor_headers, lawyer_a, court):
    case = _make_case(db, lawyer_a, court, "C-51-2026", matriz="M2")

    assert client.get("/api/v1/asignacion/desajustes", headers=auditor_headers).status_code == 200
    assert client.post(
        "/api/v1/asignacion/reasignar",
        json={"case_ids": [case.id], "lawyer_id": lawyer_a.id},
        headers=auditor_headers,
    ).status_code == 403


# ---------------------------------------------------------------------------
# GET /cases?asignado=
# ---------------------------------------------------------------------------


def test_cases_list_asignado_filter(client, db, admin_headers, lawyer_a, lawyer_b, court):
    reassigned = _make_case(db, lawyer_a, court, "C-60-2026")
    reassigned.assigned_lawyer_id = lawyer_b.id
    reassigned.assigned_at = datetime.utcnow()
    not_reassigned = _make_case(db, lawyer_a, court, "C-61-2026")
    db.commit()
    # admin role already resolves to ALL_CASES scope (see resolve_case_scope).

    resp_true = client.get("/api/v1/cases", params={"asignado": "true"}, headers=admin_headers)
    rols_true = {c["rol"] for c in resp_true.json()["items"]}
    assert "C-60-2026" in rols_true
    assert "C-61-2026" not in rols_true

    resp_false = client.get("/api/v1/cases", params={"asignado": "false"}, headers=admin_headers)
    rols_false = {c["rol"] for c in resp_false.json()["items"]}
    assert "C-61-2026" in rols_false
    assert "C-60-2026" not in rols_false

    item = next(c for c in resp_true.json()["items"] if c["rol"] == "C-60-2026")
    assert item["assigned_lawyer_id"] == lawyer_b.id
    assert item["assigned_lawyer_nombre"] == lawyer_b.name
    assert item["asignado"] is True


# ---------------------------------------------------------------------------
# A lawyer's own case list follows the assignment
# ---------------------------------------------------------------------------


def test_lawyer_case_list_follows_assignment(client, db, admin_headers, lawyer_a, lawyer_b, court, lawyer_a_headers, lawyer_b_headers):
    """A causa with no litigantes yet (bootstrap window) is visible to its
    lawyer_id owner by default; reassigning it must move that visibility to
    the new assignee, not leave it (or duplicate it) on the original owner."""
    case = _make_case(db, lawyer_a, court, "C-70-2026")

    before_a = client.get("/api/v1/cases", headers=lawyer_a_headers).json()
    assert any(c["rol"] == "C-70-2026" for c in before_a["items"])

    resp = client.post(
        "/api/v1/asignacion/reasignar",
        json={"case_ids": [case.id], "lawyer_id": lawyer_b.id},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    after_a = client.get("/api/v1/cases", headers=lawyer_a_headers).json()
    assert not any(c["rol"] == "C-70-2026" for c in after_a["items"])

    after_b = client.get("/api/v1/cases", headers=lawyer_b_headers).json()
    assert any(c["rol"] == "C-70-2026" for c in after_b["items"])
