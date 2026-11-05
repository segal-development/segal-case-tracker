"""GET /api/v1/cases/{id} -> ``liberacion``: the case's most recent release request."""
from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.liberacion import (
    LIB_APLICADO,
    LIB_PENDIENTE,
    LIB_RECHAZADO,
    LiberacionRequest,
)

ADMIN_RUT = "16021492-9"


@pytest.fixture
def case(db):
    admin = Lawyer(rut=ADMIN_RUT, name="Carla Dir", role="admin")
    court = Court(code="T1-CL", name="Juzgado Civil", region="RM", type="civil")
    db.add_all([admin, court])
    db.commit()
    c = Case(lawyer_id=admin.id, court_id=court.id, rol="C-1-2026", status="active",
             competencia="civil", semaforo="rojo")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _get(client, case_id):
    h = {"Authorization": "Bearer " + create_access_token({"sub": ADMIN_RUT})}
    r = client.get(f"/api/v1/cases/{case_id}", headers=h)
    assert r.status_code == 200
    return r.json()["liberacion"]


def _lib(db, case, estado, created, **kw):
    row = LiberacionRequest(case_id=case.id, target_semaforo="gris", motivo="acuerdo",
                            requested_by_name="Pablo", estado=estado, created_at=created, **kw)
    db.add(row)
    db.commit()
    return row


def test_none_without_requests(client, case):
    assert _get(client, case.id) is None


def test_pending(client, db, case):
    row = _lib(db, case, LIB_PENDIENTE, datetime(2026, 3, 1))
    lib = _get(client, case.id)
    assert lib["id"] == row.id and lib["estado"] == "pendiente"
    assert lib["target_semaforo"] == "gris" and lib["motivo"] == "acuerdo"
    assert lib["requested_by_name"] == "Pablo"
    assert lib["vigente"] is False and lib["approved_by_name"] is None
    assert lib["aplicado_at"] is None and lib["rechazado_by_name"] is None


def test_applied_and_vigente(client, db, case):
    at = datetime(2026, 3, 2)
    _lib(db, case, LIB_APLICADO, datetime(2026, 3, 1), aplicado_at=at,
         auditor_aprobado_by_name="Denisse")
    case.semaforo_override = "gris"
    db.commit()
    lib = _get(client, case.id)
    assert lib["estado"] == "aplicado" and lib["vigente"] is True
    assert lib["approved_by_name"] == "Denisse"
    assert lib["aplicado_at"].startswith("2026-03-02")


def test_applied_by_direccion_name(client, db, case):
    _lib(db, case, LIB_APLICADO, datetime(2026, 3, 1), aplicado_at=datetime(2026, 3, 2),
         direccion_aprobado_by_name="Carla Dir")
    assert _get(client, case.id)["approved_by_name"] == "Carla Dir"


def test_applied_but_expired_not_vigente(client, db, case):
    _lib(db, case, LIB_APLICADO, datetime(2026, 3, 1), aplicado_at=datetime(2026, 3, 2))
    assert case.semaforo_override is None
    assert _get(client, case.id)["vigente"] is False


def test_rejected(client, db, case):
    _lib(db, case, LIB_RECHAZADO, datetime(2026, 3, 1),
         rechazado_by_name="Carla Dir", rechazo_motivo="no corresponde")
    lib = _get(client, case.id)
    assert lib["estado"] == "rechazado" and lib["vigente"] is False
    assert lib["rechazado_by_name"] == "Carla Dir" and lib["rechazo_motivo"] == "no corresponde"


def test_most_recent_wins(client, db, case):
    _lib(db, case, LIB_RECHAZADO, datetime(2026, 3, 1))
    new = _lib(db, case, LIB_PENDIENTE, datetime(2026, 3, 1) + timedelta(days=1))
    assert _get(client, case.id)["id"] == new.id
