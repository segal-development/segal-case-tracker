"""GET/POST/DELETE /api/v1/admin/demandas-publicas/link — admin only."""
from datetime import timedelta

import pytest

from app.core.security import create_access_token
from app.models.lawyer import Lawyer
from app.models.public_link_token import PublicLinkToken

URL = "/api/v1/admin/demandas-publicas/link"
PUB = "/api/v1/publico/demandas/"


def _headers(db, rut, role):
    db.add(Lawyer(rut=rut, name=role, email=f"{role}@segal.cl", role=role))
    db.commit()
    token = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(db):
    return _headers(db, "11111111-1", "admin")


def test_get_without_link_is_inactive(client, admin):
    r = client.get(URL, headers=admin)
    assert r.status_code == 200
    assert r.json() == {"activo": False, "creado_at": None, "last_used_at": None}


def test_post_returns_token_once_and_stores_only_hash(client, admin, db):
    r = client.post(URL, headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"token"} and len(body["token"]) >= 43
    row = db.query(PublicLinkToken).one()
    assert row.purpose == "demandas_publicas"
    assert row.token_hash != body["token"] and len(row.token_hash) == 64
    assert row.revoked_at is None and row.created_by is not None
    assert client.get(PUB + body["token"]).status_code == 200


def test_get_never_returns_token(client, admin):
    token = client.post(URL, headers=admin).json()["token"]
    r = client.get(URL, headers=admin)
    assert r.status_code == 200
    assert set(r.json()) == {"activo", "creado_at", "last_used_at"}
    assert r.json()["activo"] is True and r.json()["creado_at"] is not None
    assert token not in r.text
    assert "token" not in r.json()


def test_post_rotates_and_invalidates_previous(client, admin, db):
    old = client.post(URL, headers=admin).json()["token"]
    new = client.post(URL, headers=admin).json()["token"]
    assert old != new
    r = client.get(PUB + old)
    assert r.status_code == 404 and r.json() == {"detail": "Link no válido."}
    assert client.get(PUB + new).status_code == 200
    rows = db.query(PublicLinkToken).all()
    assert len(rows) == 2 and sum(1 for x in rows if x.revoked_at is None) == 1


def test_delete_revokes_active_link(client, admin):
    token = client.post(URL, headers=admin).json()["token"]
    r = client.delete(URL, headers=admin)
    assert r.status_code == 204 and r.content == b""
    assert client.get(URL, headers=admin).json()["activo"] is False
    assert client.get(PUB + token).status_code == 404


def test_delete_without_link_is_204(client, admin):
    assert client.delete(URL, headers=admin).status_code == 204


def test_get_reports_last_used_at(client, admin):
    token = client.post(URL, headers=admin).json()["token"]
    assert client.get(URL, headers=admin).json()["last_used_at"] is None
    client.get(PUB + token)
    assert client.get(URL, headers=admin).json()["last_used_at"] is not None


@pytest.mark.parametrize("role", ["lawyer", "auditor", "procurador"])
def test_non_admin_403_and_nothing_changes(client, db, admin, role):
    token = client.post(URL, headers=admin).json()["token"]
    h = _headers(db, "22222222-2", role)
    assert client.get(URL, headers=h).status_code == 403
    assert client.post(URL, headers=h).status_code == 403
    assert client.delete(URL, headers=h).status_code == 403
    assert client.get(PUB + token).status_code == 200
    assert db.query(PublicLinkToken).count() == 1


def test_anonymous_rejected(client):
    assert client.get(URL).status_code in (401, 403)
    assert client.post(URL).status_code in (401, 403)
    assert client.delete(URL).status_code in (401, 403)
