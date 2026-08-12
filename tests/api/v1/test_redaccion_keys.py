"""Tests for the admin Redaccion API-key management endpoints.

Covers: create returns a plaintext key + persists exactly one row whose sha256
matches (never expecting plaintext to be stored); list hides the hash and
includes the created row; revoke flips ``is_active`` and stamps ``revoked_at``;
a revoked key is actually rejected by the EXTERNAL ``require_redaccion_key``;
and non-admins get 403 on all three endpoints.
"""

import hashlib

import pytest

from app.core.security import create_access_token
from app.models.lawyer import Lawyer
from app.models.redaccion_api_key import RedaccionApiKey

ADMIN_RUT = "16021492-9"
LAWYER_RUT = "19643548-4"

LIST_URL = "/api/v1/redaccion-keys"


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Carla Admin", role="admin")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=LAWYER_RUT, name="Fernanda Arroyo", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


# --------------------------------------------------------------------------- create


def test_create_returns_plaintext_and_persists_hashed_row(client, db, admin):
    r = client.post(LIST_URL, headers=_h(ADMIN_RUT), json={"label": "redaccion-prod"})
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "redaccion-prod"
    plaintext = body["key"]
    assert plaintext  # a non-empty plaintext key is returned once

    # exactly one row persisted, and it stores the sha256 hash (never plaintext)
    rows = db.query(RedaccionApiKey).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.key_hash == hashlib.sha256(plaintext.encode()).hexdigest()
    assert row.key_hash != plaintext  # plaintext is NOT what's stored
    assert row.is_active is True
    assert row.revoked_at is None


def test_create_trims_label_and_rejects_blank(client, db, admin):
    ok = client.post(LIST_URL, headers=_h(ADMIN_RUT), json={"label": "  redaccion  "})
    assert ok.status_code == 200
    assert ok.json()["label"] == "redaccion"

    blank = client.post(LIST_URL, headers=_h(ADMIN_RUT), json={"label": "   "})
    assert blank.status_code == 400

    empty = client.post(LIST_URL, headers=_h(ADMIN_RUT), json={"label": ""})
    assert empty.status_code == 422  # Pydantic min_length


# ----------------------------------------------------------------------------- list


def test_list_hides_hash_and_includes_created_key(client, db, admin):
    created = client.post(
        LIST_URL, headers=_h(ADMIN_RUT), json={"label": "redaccion-a"}
    ).json()

    r = client.get(LIST_URL, headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    item = items[0]
    assert item["id"] == created["id"]
    assert item["label"] == "redaccion-a"
    assert item["is_active"] is True
    assert item["revoked"] is False
    # no hash / plaintext leaks in the listing
    assert "key_hash" not in item
    assert "key" not in item


def test_list_orders_newest_first(client, db, admin):
    a = client.post(LIST_URL, headers=_h(ADMIN_RUT), json={"label": "a"}).json()
    b = client.post(LIST_URL, headers=_h(ADMIN_RUT), json={"label": "b"}).json()
    ids = [row["id"] for row in client.get(LIST_URL, headers=_h(ADMIN_RUT)).json()]
    # newest (highest id) first
    assert ids[0] == max(a["id"], b["id"])


# --------------------------------------------------------------------------- revoke


def test_revoke_flips_active_and_stamps_revoked_at(client, db, admin):
    created = client.post(
        LIST_URL, headers=_h(ADMIN_RUT), json={"label": "redaccion-x"}
    ).json()
    kid = created["id"]

    r = client.post(f"{LIST_URL}/{kid}/revoke", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    body = r.json()
    assert body["is_active"] is False
    assert body["revoked"] is True
    assert body["revoked_at"] is not None

    row = db.query(RedaccionApiKey).filter(RedaccionApiKey.id == kid).first()
    assert row.is_active is False
    assert row.revoked_at is not None


def test_revoke_unknown_id_404(client, db, admin):
    r = client.post(f"{LIST_URL}/999999/revoke", headers=_h(ADMIN_RUT))
    assert r.status_code == 404


def test_revoke_is_idempotent(client, db, admin):
    kid = client.post(
        LIST_URL, headers=_h(ADMIN_RUT), json={"label": "redaccion-y"}
    ).json()["id"]
    first = client.post(f"{LIST_URL}/{kid}/revoke", headers=_h(ADMIN_RUT))
    assert first.status_code == 200
    first_at = first.json()["revoked_at"]

    second = client.post(f"{LIST_URL}/{kid}/revoke", headers=_h(ADMIN_RUT))
    assert second.status_code == 200
    assert second.json()["is_active"] is False
    # revoked_at is preserved, not re-stamped
    assert second.json()["revoked_at"] == first_at


def test_revoked_key_is_rejected_by_external_dependency(client, db, admin):
    """End-to-end proof that revoking actually disables the key: the created
    plaintext works against the EXTERNAL Redaccion API, then stops after revoke."""
    from fastapi import HTTPException

    from app.api.redaccion.deps import require_redaccion_key

    created = client.post(
        LIST_URL, headers=_h(ADMIN_RUT), json={"label": "redaccion-e2e"}
    ).json()
    plaintext = created["key"]

    # active key is accepted by the external dependency
    accepted = require_redaccion_key(x_api_key=plaintext, db=db)
    assert accepted.id == created["id"]

    # revoke it
    client.post(f"{LIST_URL}/{created['id']}/revoke", headers=_h(ADMIN_RUT))

    # now the external dependency rejects the same plaintext
    with pytest.raises(HTTPException) as exc:
        require_redaccion_key(x_api_key=plaintext, db=db)
    assert exc.value.status_code == 401


# --------------------------------------------------------------------------- authz


def test_list_requires_admin(client, db, lawyer):
    assert client.get(LIST_URL, headers=_h(LAWYER_RUT)).status_code == 403


def test_create_requires_admin(client, db, lawyer):
    r = client.post(LIST_URL, headers=_h(LAWYER_RUT), json={"label": "nope"})
    assert r.status_code == 403


def test_revoke_requires_admin(client, db, admin, lawyer):
    kid = client.post(
        LIST_URL, headers=_h(ADMIN_RUT), json={"label": "redaccion-z"}
    ).json()["id"]
    r = client.post(f"{LIST_URL}/{kid}/revoke", headers=_h(LAWYER_RUT))
    assert r.status_code == 403
