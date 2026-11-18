"""PUT /api/v1/lawyers/{id}/account applies ``nivel`` for non-procurador accounts.

Absent nivel = unchanged. The body cannot tell "absent" from null, so clearing a nivel
goes through PUT /api/v1/bono/nivel/{lawyer_id}, not through this route.
"""
import pytest

from app.core.security import create_access_token, hash_password
from app.models.lawyer import Lawyer

ADMIN_RUT = "16021492-9"
PASSWORD = "una-clave-segura-1"


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


def _mk(db, rut, name, **kw):
    obj = Lawyer(rut=rut, name=name, password_hash=hash_password(PASSWORD), **kw)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def admin(db):
    return _mk(db, ADMIN_RUT, "Carla Admin", role="admin", email="carla@segal.cl")


@pytest.fixture
def ana(db, admin):
    return _mk(db, "12345678-5", "Ana Perez", role="lawyer", email="ana@segal.cl",
               is_active=True, nivel="junior")


def _put(client, lawyer_id, body):
    return client.put(f"/api/v1/lawyers/{lawyer_id}/account", headers=_h(ADMIN_RUT), json=body)


def _row(db, lawyer_id):
    db.expire_all()
    return db.query(Lawyer).filter(Lawyer.id == lawyer_id).one()


def test_nivel_is_applied(client, db, ana):
    r = _put(client, ana.id, {"nivel": "pleno"})
    assert r.status_code == 200
    assert r.json()["nivel"] == "pleno"
    assert _row(db, ana.id).nivel == "pleno"


def test_nivel_applied_together_with_other_fields(client, db, ana):
    r = _put(client, ana.id, {"nivel": "pleno", "email": "nueva@segal.cl"})
    assert r.status_code == 200
    assert r.json()["email"] == "nueva@segal.cl"
    assert _row(db, ana.id).nivel == "pleno"


def test_invalid_nivel_is_422_and_changes_nothing(client, db, ana):
    r = _put(client, ana.id, {"nivel": "senior", "email": "otra@segal.cl"})
    assert r.status_code == 422
    row = _row(db, ana.id)
    assert row.nivel == "junior" and row.email == "ana@segal.cl"


def test_absent_nivel_leaves_it_unchanged(client, db, ana):
    r = _put(client, ana.id, {"email": "nueva@segal.cl"})
    assert r.status_code == 200
    assert _row(db, ana.id).nivel == "junior"


def test_null_nivel_is_treated_as_absent(client, db, ana):
    r = _put(client, ana.id, {"nivel": None})
    assert r.status_code == 200
    assert _row(db, ana.id).nivel == "junior"


def test_existing_procurador_with_nivel_is_422(client, db, admin):
    proc = _mk(db, "9999999-9", "Pablo Proc", role="procurador", is_firm_lawyer=False)
    r = _put(client, proc.id, {"nivel": "junior"})
    assert r.status_code == 422
    assert r.json()["detail"] == "Un procurador no tiene nivel de bono."
    assert _row(db, proc.id).nivel is None


def test_switching_to_procurador_with_nivel_is_422_and_changes_nothing(client, db, ana, admin):
    r = _put(client, ana.id, {"role": "procurador", "nivel": "pleno", "abogado_ids": [admin.id]})
    assert r.status_code == 422
    assert r.json()["detail"] == "Un procurador no tiene nivel de bono."
    row = _row(db, ana.id)
    assert row.nivel == "junior" and row.role == "lawyer"


def test_switching_to_procurador_clears_nivel(client, db, ana, admin):
    admin.is_firm_lawyer = True
    db.commit()
    r = _put(client, ana.id, {"role": "procurador", "abogado_ids": [admin.id]})
    assert r.status_code == 200
    row = _row(db, ana.id)
    assert row.role == "procurador" and row.nivel is None and row.is_firm_lawyer is False


def test_other_fields_still_behave(client, db, ana):
    r = _put(client, ana.id, {"email": "n@segal.cl", "role": "auditor",
                              "new_password": "otra-clave-123", "is_active": False})
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "n@segal.cl" and body["role"] == "auditor"
    assert body["is_active"] is False
    row = _row(db, ana.id)
    assert row.nivel == "junior" and row.blocked_at is not None
    assert row.password_changed_at is not None
