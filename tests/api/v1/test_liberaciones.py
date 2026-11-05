"""Tests for liberación de causa — dual sign-off (auditor + dirección) semáforo
override, the role→side mapping, permission boundaries, and the override being
superseded when a newer movement lands.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.liberacion import LiberacionRequest
from app.services.deadline_engine import DeadlineEngine

ADMIN_RUT = "16021492-9"
AUDITOR_RUT = "auditor-1"
LAWYER_RUT = "19456852-5"


@pytest.fixture
def actors(db):
    a = Lawyer(rut=ADMIN_RUT, name="Carla Dir", role="admin")
    au = Lawyer(rut=AUDITOR_RUT, name="Denisse Aud", role="auditor")
    la = Lawyer(rut=LAWYER_RUT, name="Pablo Lawyer", role="lawyer")
    db.add_all([a, au, la])
    db.commit()
    return {"admin": a, "auditor": au, "lawyer": la}


@pytest.fixture
def case(db, actors):
    court = Court(code="T1-LIB", name="Juzgado Civil", region="RM", type="civil")
    db.add(court)
    db.commit()
    c = Case(lawyer_id=actors["lawyer"].id, court_id=court.id, rol="C-777-2026",
             status="active", competencia="civil", semaforo="rojo",
             plaintiff="BANCO", defendant="DEUDOR")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


def _request(client, case_id, target="amarillo"):
    return client.post("/api/v1/liberaciones", headers=_h(LAWYER_RUT),
                       json={"case_id": case_id, "target_semaforo": target, "motivo": "negociación"})


def test_admin_single_signature_applies(client, db, actors, case):
    lib_id = _request(client, case.id).json()["id"]
    r = client.post(f"/api/v1/liberaciones/{lib_id}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 200 and r.json()["estado"] == "aplicado"
    assert r.json()["direccion_ok"] is True and r.json()["auditor_ok"] is False
    db.refresh(case)
    assert case.semaforo == "amarillo"
    assert case.semaforo_override == "amarillo"
    assert case.semaforo_override_by == "Carla Dir"
    assert case.semaforo_override_at is not None


def test_auditor_single_signature_applies(client, db, actors, case):
    lib_id = _request(client, case.id).json()["id"]
    r = client.post(f"/api/v1/liberaciones/{lib_id}/aprobar", headers=_h(AUDITOR_RUT))
    assert r.status_code == 200 and r.json()["estado"] == "aplicado"
    assert r.json()["auditor_ok"] is True and r.json()["direccion_ok"] is False
    db.refresh(case)
    assert case.semaforo == "amarillo" and case.semaforo_override_by == "Denisse Aud"
    row = db.query(LiberacionRequest).get(lib_id)
    assert row.aplicado_at is not None


def test_gris_release_takes_case_out_of_semaforo(client, db, actors, case):
    r = _request(client, case.id, target="gris")
    assert r.status_code == 201 and r.json()["target_semaforo"] == "gris"
    client.post(f"/api/v1/liberaciones/{r.json()['id']}/aprobar", headers=_h(ADMIN_RUT))
    db.refresh(case)
    assert case.semaforo == "gris" and case.semaforo_override == "gris"


@pytest.mark.parametrize("motivo", [None, "", "   ", "x" * 501])
def test_motivo_required_and_bounded(client, db, actors, case, motivo):
    body = {"case_id": case.id, "target_semaforo": "gris"}
    if motivo is not None:
        body["motivo"] = motivo
    r = client.post("/api/v1/liberaciones", headers=_h(LAWYER_RUT), json=body)
    assert r.status_code == 400
    assert r.json()["detail"] == "Indica el motivo de la liberación."
    assert db.query(LiberacionRequest).count() == 0


def test_motivo_is_stripped(client, db, actors, case):
    r = client.post("/api/v1/liberaciones", headers=_h(LAWYER_RUT),
                    json={"case_id": case.id, "target_semaforo": "gris", "motivo": "  acuerdo  "})
    assert r.status_code == 201 and r.json()["motivo"] == "acuerdo"


def test_second_approve_after_applied_is_409(client, db, actors, case):
    lib_id = _request(client, case.id).json()["id"]
    assert client.post(f"/api/v1/liberaciones/{lib_id}/aprobar", headers=_h(AUDITOR_RUT)).status_code == 200
    r = client.post(f"/api/v1/liberaciones/{lib_id}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 409


def test_list_items_carry_rol_tribunal_abogado(client, db, actors, case):
    _request(client, case.id)
    r = client.get("/api/v1/liberaciones?estado=pendiente", headers=_h(ADMIN_RUT))
    item = r.json()[0]
    assert item["rol"] == "C-777-2026"
    assert item["tribunal"] == "Juzgado Civil"
    assert item["abogado_nombre"] == "Pablo Lawyer"


def test_regular_lawyer_cannot_approve(client, db, actors, case):
    lib_id = _request(client, case.id).json()["id"]
    r = client.post(f"/api/v1/liberaciones/{lib_id}/aprobar", headers=_h(LAWYER_RUT))
    assert r.status_code == 403


def test_one_pending_request_per_case(client, db, actors, case):
    assert _request(client, case.id).status_code == 201
    assert _request(client, case.id).status_code == 409


def test_reject_leaves_case_unchanged(client, db, actors, case):
    lib_id = _request(client, case.id).json()["id"]
    r = client.post(f"/api/v1/liberaciones/{lib_id}/rechazar", headers=_h(ADMIN_RUT),
                    json={"motivo": "no corresponde"})
    assert r.status_code == 200 and r.json()["estado"] == "rechazado"
    db.refresh(case)
    assert case.semaforo == "rojo"


def test_invalid_target_rejected(client, db, actors, case):
    r = client.post("/api/v1/liberaciones", headers=_h(LAWYER_RUT),
                    json={"case_id": case.id, "target_semaforo": "azul"})
    assert r.status_code == 400


def _mv(desc, day, procedure="Resolución"):
    return SimpleNamespace(procedure=procedure, description=desc, movement_date=day)


def _ov(at):
    return SimpleNamespace(semaforo_override="gris", semaforo_override_at=at,
                           semaforo_override_by="x", last_movement_at=at + timedelta(days=5))


AT = datetime(2026, 3, 10, 15, 30)


def test_override_kept_after_escrito_dated_later():
    case = _ov(AT)
    mvs = [_mv("Escrito del demandante", datetime(2026, 3, 20), procedure="Escrito")]
    assert DeadlineEngine._apply_semaforo_override(case, "rojo", mvs) == "gris"
    assert case.semaforo_override == "gris"


def test_override_kept_after_court_act_same_day():
    case = _ov(AT)
    mvs = [_mv("Resuelve", datetime(2026, 3, 10, 23, 59))]
    assert DeadlineEngine._apply_semaforo_override(case, "rojo", mvs) == "gris"


def test_override_cleared_after_court_act_later_day():
    case = _ov(AT)
    mvs = [_mv("Resuelve", datetime(2026, 3, 11, 0, 1))]
    assert DeadlineEngine._apply_semaforo_override(case, "rojo", mvs) == "rojo"
    assert case.semaforo_override is None and case.semaforo_override_at is None
    assert case.semaforo_override_by is None


def test_override_ignores_nulo_court_act():
    case = _ov(AT)
    mvs = [_mv("[Nulo] Resuelve", datetime(2026, 3, 20))]
    assert DeadlineEngine._apply_semaforo_override(case, "rojo", mvs) == "gris"


def test_override_without_movements_holds():
    case = _ov(AT)
    assert DeadlineEngine._apply_semaforo_override(case, "rojo", []) == "gris"
    assert DeadlineEngine._apply_semaforo_override(case, "rojo") == "gris"
