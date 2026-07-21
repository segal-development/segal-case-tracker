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


def test_dual_signoff_applies_override(client, db, actors, case):
    lib_id = _request(client, case.id).json()["id"]

    # Only the auditor: still pending, case unchanged.
    r = client.post(f"/api/v1/liberaciones/{lib_id}/aprobar", headers=_h(AUDITOR_RUT))
    assert r.status_code == 200 and r.json()["estado"] == "pendiente"
    assert r.json()["auditor_ok"] is True and r.json()["direccion_ok"] is False
    db.refresh(case)
    assert case.semaforo == "rojo"  # not moved yet

    # Dirección completes the pair → applied, case moves, override recorded.
    r = client.post(f"/api/v1/liberaciones/{lib_id}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 200 and r.json()["estado"] == "aplicado"
    db.refresh(case)
    assert case.semaforo == "amarillo"
    assert case.semaforo_override == "amarillo"
    assert "Carla" in (case.semaforo_override_by or "") and "Denisse" in (case.semaforo_override_by or "")


def test_regular_lawyer_cannot_approve(client, db, actors, case):
    lib_id = _request(client, case.id).json()["id"]
    r = client.post(f"/api/v1/liberaciones/{lib_id}/aprobar", headers=_h(LAWYER_RUT))
    assert r.status_code == 403


def test_same_role_cannot_sign_twice(client, db, actors, case):
    lib_id = _request(client, case.id).json()["id"]
    client.post(f"/api/v1/liberaciones/{lib_id}/aprobar", headers=_h(AUDITOR_RUT))
    r = client.post(f"/api/v1/liberaciones/{lib_id}/aprobar", headers=_h(AUDITOR_RUT))
    assert r.status_code == 409  # auditor already signed


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


def test_override_superseded_by_newer_movement():
    """The engine drops the override once a movement lands after it."""
    now = datetime.utcnow()
    # No newer movement → override holds.
    held = SimpleNamespace(semaforo_override="verde", semaforo_override_at=now,
                           last_movement_at=now - timedelta(days=2))
    assert DeadlineEngine._apply_semaforo_override(held, "rojo") == "verde"

    # Newer movement → override cleared, computed color resumes.
    stale = SimpleNamespace(semaforo_override="verde", semaforo_override_at=now - timedelta(days=2),
                            last_movement_at=now)
    assert DeadlineEngine._apply_semaforo_override(stale, "rojo") == "rojo"
    assert stale.semaforo_override is None
