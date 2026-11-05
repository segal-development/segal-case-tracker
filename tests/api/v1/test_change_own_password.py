"""Tests for POST /api/v1/auth/change-password (self-service password change).

Any authenticated user can change THEIR OWN app password. The target account
always comes from the JWT, never from the request body.
"""

import logging

import pytest

from app.core.security import create_access_token, hash_password, verify_password
from app.models.lawyer import Lawyer

URL = "/api/v1/auth/change-password"
GENERIC_ERROR = "Credenciales inválidas"


def _make_lawyer(db, rut, email, password, role="lawyer"):
    lawyer = Lawyer(rut=rut, name=f"Lawyer {rut}", email=email, role=role)
    lawyer.password_hash = hash_password(password) if password else None
    db.add(lawyer)
    db.commit()
    db.refresh(lawyer)
    return lawyer


def _auth(rut):
    return {"Authorization": f"Bearer {create_access_token({'sub': rut})}"}


def _login(client, email, password):
    return client.post("/api/v1/auth/web-login", json={"email": email, "password": password})


@pytest.fixture
def user(db):
    return _make_lawyer(db, "11111111-1", "user@segal.cl", "oldpassword1")


def test_changes_password_and_only_new_one_logs_in(client, user):
    resp = client.post(
        URL,
        json={"current_password": "oldpassword1", "new_password": "brandnewpass2"},
        headers=_auth(user.rut),
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    assert _login(client, "user@segal.cl", "brandnewpass2").status_code == 200
    assert _login(client, "user@segal.cl", "oldpassword1").status_code == 401


def test_wrong_current_password_fails_and_does_not_change(client, db, user):
    resp = client.post(
        URL,
        json={"current_password": "not-the-password", "new_password": "brandnewpass2"},
        headers=_auth(user.rut),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == GENERIC_ERROR

    db.refresh(user)
    assert verify_password("oldpassword1", user.password_hash)
    assert _login(client, "user@segal.cl", "oldpassword1").status_code == 200
    assert _login(client, "user@segal.cl", "brandnewpass2").status_code == 401


def test_body_identity_cannot_change_someone_elses_password(client, db, user):
    victim = _make_lawyer(db, "22222222-2", "victim@segal.cl", "victimpass99")
    resp = client.post(
        URL,
        json={
            "current_password": "oldpassword1",
            "new_password": "brandnewpass2",
            "id": victim.id,
            "lawyer_id": victim.id,
            "rut": victim.rut,
            "email": victim.email,
        },
        headers=_auth(user.rut),
    )
    assert resp.status_code == 200

    db.refresh(victim)
    assert verify_password("victimpass99", victim.password_hash)
    assert _login(client, "victim@segal.cl", "victimpass99").status_code == 200
    assert _login(client, "user@segal.cl", "brandnewpass2").status_code == 200


def test_victim_current_password_does_not_work_for_attacker_session(client, db, user):
    """Knowing the victim's password is irrelevant: the session owner is verified."""
    victim = _make_lawyer(db, "22222222-2", "victim@segal.cl", "victimpass99")
    resp = client.post(
        URL,
        json={"current_password": "victimpass99", "new_password": "brandnewpass2", "id": victim.id},
        headers=_auth(user.rut),
    )
    assert resp.status_code == 400
    db.refresh(victim)
    assert verify_password("victimpass99", victim.password_hash)


def test_new_password_shorter_than_8_is_rejected(client, db, user):
    resp = client.post(
        URL,
        json={"current_password": "oldpassword1", "new_password": "short7!"},
        headers=_auth(user.rut),
    )
    assert resp.status_code == 422
    db.refresh(user)
    assert verify_password("oldpassword1", user.password_hash)


def test_new_password_equal_to_current_is_rejected(client, db, user):
    resp = client.post(
        URL,
        json={"current_password": "oldpassword1", "new_password": "oldpassword1"},
        headers=_auth(user.rut),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "La contraseña nueva debe ser distinta de la actual"
    db.refresh(user)
    assert verify_password("oldpassword1", user.password_hash)


def test_user_without_password_is_rejected_with_clear_message(client, db):
    nopwd = _make_lawyer(db, "33333333-3", "nopwd@segal.cl", None)
    resp = client.post(
        URL,
        json={"current_password": "anything-here", "new_password": "brandnewpass2"},
        headers=_auth(nopwd.rut),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Tu cuenta no tiene contraseña configurada; pide a un administrador que la establezca"
    )
    db.refresh(nopwd)
    assert nopwd.password_hash is None


def test_requires_authentication(client, user):
    # No header -> 401, like every other protected endpoint (the /auth routes now share the
    # dependency that also refuses a blocked person).
    resp = client.post(
        URL, json={"current_password": "oldpassword1", "new_password": "brandnewpass2"}
    )
    assert resp.status_code == 401


def test_invalid_token_is_401(client, user):
    resp = client.post(
        URL,
        json={"current_password": "oldpassword1", "new_password": "brandnewpass2"},
        headers={"Authorization": "Bearer garbage"},
    )
    assert resp.status_code == 401


def test_passwords_are_never_logged(client, user, caplog):
    caplog.set_level(logging.DEBUG)
    client.post(
        URL,
        json={"current_password": "wrong-secret-1", "new_password": "secret-new-pass-2"},
        headers=_auth(user.rut),
    )
    client.post(
        URL,
        json={"current_password": "oldpassword1", "new_password": "secret-new-pass-2"},
        headers=_auth(user.rut),
    )
    text = caplog.text
    for secret in ("wrong-secret-1", "secret-new-pass-2", "oldpassword1"):
        assert secret not in text
