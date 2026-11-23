"""POST/GET /api/v1/admin/estacion/reinicio — admin only."""

from datetime import timedelta

import pytest

from app.core.security import create_access_token
from app.models.lawyer import Lawyer

URL = "/api/v1/admin/estacion/reinicio"


def _headers(db, rut, role):
    db.add(Lawyer(rut=rut, name=role, email=f"{role}@segal.cl", role=role))
    db.commit()
    token = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(db):
    return _headers(db, "11111111-1", "admin")


def test_post_sets_request_and_returns_pending(client, admin):
    r = client.post(URL, headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert body["pendiente"] is True
    assert isinstance(body["solicitado_at"], int)
    assert set(body) == {"solicitado_at", "pendiente"}


def test_post_is_idempotent_while_pending(client, admin):
    first = client.post(URL, headers=admin).json()
    second = client.post(URL, headers=admin).json()
    assert second == first


def test_get_returns_state(client, admin):
    assert client.get(URL, headers=admin).json() == {
        "solicitado_at": None, "hecho_at": None, "pendiente": False,
    }
    sol = client.post(URL, headers=admin).json()["solicitado_at"]
    assert client.get(URL, headers=admin).json() == {
        "solicitado_at": sol, "hecho_at": None, "pendiente": True,
    }


@pytest.mark.parametrize("role", ["lawyer", "auditor"])
def test_non_admin_gets_403_and_nothing_is_stored(client, db, role):
    h = _headers(db, "22222222-2", role)
    assert client.post(URL, headers=h).status_code == 403
    assert client.get(URL, headers=h).status_code == 403
    from app.services import estacion_reinicio
    assert estacion_reinicio.pendiente(db) is False


def test_anonymous_is_rejected(client):
    assert client.post(URL).status_code in (401, 403)
