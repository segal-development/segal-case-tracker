"""Traspaso de cartera — apply / revert / list endpoints and the history that
POST /asignacion/reasignar now writes."""
from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_assignment_history import CaseAssignmentHistory
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services import lawyer_roster, traspaso_cartera

ADMIN_RUT = "11111111-1"
BASE = "/api/v1/asignacion"


def _headers(rut):
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


def _lawyer(db, rut, name, nivel="pleno", **kw):
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
    obj = Court(code="T1-TRAPL", name="Juzgado Apply", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def origen(db):
    return _lawyer(db, "10000001-1", "Origen Uno")


@pytest.fixture
def dest_a(db):
    return _lawyer(db, "10000002-2", "Dest A")


@pytest.fixture
def dest_b(db):
    return _lawyer(db, "10000003-3", "Dest B")


def _case(db, owner, court, rol, assigned=None, **kw):
    obj = Case(
        lawyer_id=owner.id, court_id=court.id, rol=rol, status="active", competencia="civil",
        matriz="M2", matriz_origen="pjud_etapa", assigned_lawyer_id=assigned.id if assigned else None,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(), **kw,
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _apply(client, origen, pairs, bloquear=False, rut=ADMIN_RUT):
    body = {
        "origen_lawyer_id": origen.id,
        "asignaciones": [{"case_id": c, "destino_lawyer_id": d} for c, d in pairs],
        "bloquear_origen": bloquear,
    }
    return client.post(f"{BASE}/traspaso/aplicar", json=body, headers=_headers(rut))


# --- apply -----------------------------------------------------------------


def test_apply_writes_override_history_and_motivo(client, db, admin, origen, dest_a, dest_b, court):
    c1 = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    c2 = _case(db, dest_a, court, "C-2-2026", assigned=origen)
    r = _apply(client, origen, [(c1.id, dest_a.id), (c2.id, dest_b.id)])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["aplicadas"] == 2 and body["bloqueada"] is False and body["batch_id"]
    db.expire_all()
    for case, dest in ((c1, dest_a), (c2, dest_b)):
        fresh = db.get(Case, case.id)
        assert fresh.assigned_lawyer_id == dest.id
        assert fresh.assigned_by_rut == ADMIN_RUT
        assert fresh.assigned_at is not None
        assert fresh.assigned_motivo == "Traspaso de cartera de Origen Uno"
        assert fresh.lawyer_id == dest_a.id  # provenance untouched
    rows = db.query(CaseAssignmentHistory).order_by(CaseAssignmentHistory.case_id).all()
    assert len(rows) == 2
    assert {r.batch_id for r in rows} == {body["batch_id"]}
    row = rows[0]
    assert (row.from_lawyer_id, row.prev_assigned_lawyer_id, row.to_lawyer_id) == (origen.id, origen.id, dest_a.id)
    assert row.created_by_rut == ADMIN_RUT and row.reverted_at is None
    assert row.motivo == "Traspaso de cartera de Origen Uno"


def test_apply_invalidates_scope_cache(client, db, admin, origen, dest_a, court):
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    lawyer_roster._scope_cache["sentinel"] = (float("inf"), frozenset({c.id}))
    assert _apply(client, origen, [(c.id, dest_a.id)]).status_code == 200
    assert "sentinel" not in lawyer_roster._scope_cache


def test_apply_bloquear_origen(client, db, admin, origen, dest_a, court):
    origen.hito_form_token = "tok"
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    db.commit()
    r = _apply(client, origen, [(c.id, dest_a.id)], bloquear=True)
    assert r.status_code == 200 and r.json()["bloqueada"] is True
    db.expire_all()
    fresh = db.get(Lawyer, origen.id)
    assert fresh.is_active is False and fresh.blocked_at is not None and fresh.hito_form_token is None


def test_apply_refuses_to_block_the_admin_herself_and_writes_nothing(client, db, admin, dest_a, court):
    """An admin who hands over her own cartera must not lock herself out (same rule as the
    account endpoint: 'No puedes bloquear tu propia cuenta'). Verifier finding."""
    admin.is_firm_lawyer = True
    admin.nivel = "pleno"
    db.commit()
    c = _case(db, dest_a, court, "C-1-2026", assigned=admin)
    r = _apply(client, admin, [(c.id, dest_a.id)], bloquear=True)
    assert r.status_code == 409
    assert r.json()["detail"] == "No puedes bloquear tu propia cuenta"
    db.expire_all()
    assert db.get(Lawyer, admin.id).is_active is not False
    assert db.get(Case, c.id).assigned_lawyer_id == admin.id
    assert db.query(CaseAssignmentHistory).count() == 0


def test_apply_own_cartera_without_blocking_is_allowed(client, db, admin, dest_a, court):
    admin.is_firm_lawyer = True
    admin.nivel = "pleno"
    db.commit()
    c = _case(db, dest_a, court, "C-1-2026", assigned=admin)
    assert _apply(client, admin, [(c.id, dest_a.id)]).status_code == 200


def test_apply_without_bloquear_keeps_account(client, db, admin, origen, dest_a, court):
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    _apply(client, origen, [(c.id, dest_a.id)])
    db.expire_all()
    assert db.get(Lawyer, origen.id).is_active is True


def test_apply_409_when_case_changed_owner_and_nothing_written(client, db, admin, origen, dest_a, dest_b, court):
    mine = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    moved = _case(db, dest_a, court, "C-2-2026", assigned=dest_b)
    r = _apply(client, origen, [(mine.id, dest_a.id), (moved.id, dest_a.id)], bloquear=True)
    assert r.status_code == 409
    assert r.json()["detail"]["case_ids"] == [moved.id]
    db.expire_all()
    assert db.get(Case, mine.id).assigned_lawyer_id == origen.id
    assert db.query(CaseAssignmentHistory).count() == 0
    assert db.get(Lawyer, origen.id).is_active is True


def test_apply_422s(client, db, admin, origen, dest_a, court):
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    blocked = _lawyer(db, "10000009-9", "Blocked", is_active=False)
    external = _lawyer(db, "10000008-8", "External")
    external.is_firm_lawyer = False
    db.commit()
    assert _apply(client, origen, [(c.id, blocked.id)]).status_code == 422
    assert _apply(client, origen, [(c.id, external.id)]).status_code == 422
    assert _apply(client, origen, [(c.id, 9999)]).status_code == 422
    assert _apply(client, origen, [(c.id, origen.id)]).status_code == 422
    assert _apply(client, origen, [(c.id, dest_a.id), (c.id, dest_a.id)]).status_code == 422
    assert _apply(client, origen, [(9999, dest_a.id)]).status_code == 422
    assert client.post(f"{BASE}/traspaso/aplicar", headers=_headers(ADMIN_RUT), json={
        "origen_lawyer_id": 9999, "asignaciones": [], "bloquear_origen": False}).status_code == 422
    db.expire_all()
    assert db.get(Case, c.id).assigned_lawyer_id == origen.id
    assert db.query(CaseAssignmentHistory).count() == 0


def test_apply_is_all_or_nothing_on_failure(db, admin, origen, dest_a, court, monkeypatch):
    c1 = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    c2 = _case(db, dest_a, court, "C-2-2026", assigned=origen)
    calls = []
    real = traspaso_cartera.record_history

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*a, **kw)

    monkeypatch.setattr(traspaso_cartera, "record_history", flaky)
    with pytest.raises(RuntimeError):
        traspaso_cartera.apply_traspaso(
            db, origen.id, [(c1.id, dest_a.id), (c2.id, dest_a.id)], actor_rut=ADMIN_RUT)
    db.expire_all()
    assert db.get(Case, c1.id).assigned_lawyer_id == origen.id
    assert db.get(Case, c2.id).assigned_lawyer_id == origen.id
    assert db.query(CaseAssignmentHistory).count() == 0


def test_apply_owner_by_litigante_stores_null_previous_override(client, db, admin, origen, dest_a, court):
    from app.models.case_litigante import CaseLitigante
    c = _case(db, dest_a, court, "C-1-2026")
    db.add(CaseLitigante(case_id=c.id, participante="AB.DDO", rut=origen.rut,
                         persona_type="NATURAL", nombre="Origen", natural_key="k"))
    db.commit()
    assert _apply(client, origen, [(c.id, dest_a.id)]).status_code == 200
    row = db.query(CaseAssignmentHistory).one()
    assert row.from_lawyer_id == origen.id
    assert row.prev_assigned_lawyer_id is None and row.prev_assigned_at is None
    assert row.prev_assigned_by_rut is None and row.prev_assigned_motivo is None


# --- revert ----------------------------------------------------------------


def test_revert_restores_null_and_previous_override(client, db, admin, origen, dest_a, dest_b, court):
    when = datetime(2026, 1, 2, 3, 4, 5)
    by_lit = _case(db, dest_a, court, "C-1-2026")
    by_lit.assigned_lawyer_id = origen.id
    by_lit.assigned_at, by_lit.assigned_by_rut, by_lit.assigned_motivo = when, "5-5", "manual"
    plain = _case(db, dest_a, court, "C-2-2026")
    from app.models.case_litigante import CaseLitigante
    db.add(CaseLitigante(case_id=plain.id, participante="AB.DDO", rut=origen.rut,
                         persona_type="NATURAL", nombre="Origen", natural_key="k"))
    db.commit()
    batch = _apply(client, origen, [(by_lit.id, dest_b.id), (plain.id, dest_b.id)]).json()["batch_id"]
    r = client.post(f"{BASE}/traspaso/{batch}/revertir", headers=_headers(ADMIN_RUT))
    assert r.status_code == 200, r.text
    assert r.json()["revertidas"] == 2 and r.json()["omitidas"] == []
    db.expire_all()
    a = db.get(Case, by_lit.id)
    assert (a.assigned_lawyer_id, a.assigned_at, a.assigned_by_rut, a.assigned_motivo) == (origen.id, when, "5-5", "manual")
    b = db.get(Case, plain.id)
    assert (b.assigned_lawyer_id, b.assigned_at, b.assigned_by_rut, b.assigned_motivo) == (None, None, None, None)
    assert all(h.reverted_at is not None for h in db.query(CaseAssignmentHistory))


def test_revert_skips_cases_changed_since_and_does_not_unblock(client, db, admin, origen, dest_a, dest_b, court):
    c1 = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    c2 = _case(db, dest_a, court, "C-2-2026", assigned=origen)
    batch = _apply(client, origen, [(c1.id, dest_a.id), (c2.id, dest_a.id)], bloquear=True).json()["batch_id"]
    c2 = db.get(Case, c2.id)
    c2.assigned_lawyer_id = dest_b.id
    c2.assigned_motivo = "manual"
    db.commit()
    r = client.post(f"{BASE}/traspaso/{batch}/revertir", headers=_headers(ADMIN_RUT))
    assert r.status_code == 200
    body = r.json()
    assert body["revertidas"] == 1
    assert [o["case_id"] for o in body["omitidas"]] == [c2.id]
    db.expire_all()
    assert db.get(Case, c1.id).assigned_lawyer_id == origen.id
    assert db.get(Case, c2.id).assigned_lawyer_id == dest_b.id
    assert db.get(Lawyer, origen.id).is_active is False


def test_revert_twice_and_unknown_batch(client, db, admin, origen, dest_a, court):
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    batch = _apply(client, origen, [(c.id, dest_a.id)]).json()["batch_id"]
    assert client.post(f"{BASE}/traspaso/{batch}/revertir", headers=_headers(ADMIN_RUT)).status_code == 200
    assert client.post(f"{BASE}/traspaso/{batch}/revertir", headers=_headers(ADMIN_RUT)).status_code == 409
    assert client.post(f"{BASE}/traspaso/nope/revertir", headers=_headers(ADMIN_RUT)).status_code == 404


def test_revert_invalidates_scope_cache(client, db, admin, origen, dest_a, court):
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    batch = _apply(client, origen, [(c.id, dest_a.id)]).json()["batch_id"]
    lawyer_roster._scope_cache["sentinel"] = (float("inf"), frozenset())
    client.post(f"{BASE}/traspaso/{batch}/revertir", headers=_headers(ADMIN_RUT))
    assert "sentinel" not in lawyer_roster._scope_cache


# --- list ------------------------------------------------------------------


def test_list_batches(client, db, admin, origen, dest_a, court):
    c1 = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    c2 = _case(db, dest_a, court, "C-2-2026", assigned=origen)
    b1 = _apply(client, origen, [(c1.id, dest_a.id)]).json()["batch_id"]
    b2 = _apply(client, origen, [(c2.id, dest_a.id)]).json()["batch_id"]
    client.post(f"{BASE}/traspaso/{b1}/revertir", headers=_headers(ADMIN_RUT))
    r = client.get(f"{BASE}/traspaso", headers=_headers(ADMIN_RUT))
    assert r.status_code == 200
    got = {b["batch_id"]: b for b in r.json()}
    assert got[b1]["reverted"] is True and got[b2]["reverted"] is False
    assert got[b2]["count"] == 1 and got[b2]["created_by"] == ADMIN_RUT
    assert got[b2]["origen_lawyer_id"] == origen.id and got[b2]["origen_nombre"] == "Origen Uno"
    assert got[b2]["created_at"]


# --- reasignar writes history ---------------------------------------------


def test_reasignar_writes_history(client, db, admin, origen, dest_a, dest_b, court):
    c = _case(db, dest_a, court, "C-1-2026", assigned=origen)
    r = client.post(f"{BASE}/reasignar", headers=_headers(ADMIN_RUT),
                    json={"case_ids": [c.id], "lawyer_id": dest_b.id, "motivo": "manual"})
    assert r.status_code == 200 and r.json()["reasignadas"] == 1
    row = db.query(CaseAssignmentHistory).one()
    assert (row.case_id, row.from_lawyer_id, row.prev_assigned_lawyer_id, row.to_lawyer_id) == (
        c.id, origen.id, origen.id, dest_b.id)
    assert row.motivo == "manual" and row.created_by_rut == ADMIN_RUT and row.batch_id


# --- auth ------------------------------------------------------------------


@pytest.mark.parametrize("role", ["lawyer", "auditor"])
def test_non_admin_forbidden(client, db, origen, role):
    db.add(Lawyer(rut="20000001-1", name="X", role=role, is_firm_lawyer=False)); db.commit()
    h = _headers("20000001-1")
    assert client.post(f"{BASE}/traspaso/aplicar", headers=h, json={
        "origen_lawyer_id": origen.id, "asignaciones": [], "bloquear_origen": False}).status_code == 403
    assert client.post(f"{BASE}/traspaso/abc/revertir", headers=h).status_code == 403
    assert client.get(f"{BASE}/traspaso", headers=h).status_code == 403
