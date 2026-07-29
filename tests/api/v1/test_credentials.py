"""Tests for the self-service PJUD credential update — PUT /credentials/me."""

from datetime import datetime

import pytest

from app.core.security import create_access_token
from app.models.lawyer import Lawyer

LAWYER_RUT = "18888888-8"


@pytest.fixture
def lawyer(db):
    obj = Lawyer(
        rut=LAWYER_RUT,
        name="Ana Firma",
        email="ana@segal.cl",
        role="lawyer",
        is_firm_lawyer=True,
        is_active=True,
        preferred_auth_method="clave_unica",
        credential_alert_sent_at=datetime.utcnow(),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


def test_lawyer_updates_own_clave_unica(client, db, lawyer):
    r = client.put(
        "/api/v1/credentials/me", headers=_h(LAWYER_RUT),
        json={"password": "nuevaClave123", "auth_method": "clave_unica"},
    )
    assert r.status_code == 200
    assert r.json()["auth_method"] == "clave_unica"
    assert "nuevaClave123" not in r.text  # plaintext never echoed
    db.refresh(lawyer)
    assert lawyer.encrypted_clave_unica_password is not None
    assert lawyer.preferred_auth_method == "clave_unica"
    # The alert de-dup marker is reset so a future failure re-alerts.
    assert lawyer.credential_alert_sent_at is None


def test_defaults_to_preferred_method(client, db, lawyer):
    r = client.put(
        "/api/v1/credentials/me", headers=_h(LAWYER_RUT),
        json={"password": "otraClave99"},  # no auth_method → preferred
    )
    assert r.status_code == 200
    assert r.json()["auth_method"] == "clave_unica"
    db.refresh(lawyer)
    assert lawyer.encrypted_clave_unica_password is not None


def test_captcha_method_sets_pjud_slot(client, db, lawyer):
    r = client.put(
        "/api/v1/credentials/me", headers=_h(LAWYER_RUT),
        json={"password": "claveCaptcha", "auth_method": "captcha"},
    )
    assert r.status_code == 200
    db.refresh(lawyer)
    assert lawyer.encrypted_pjud_password is not None
    assert lawyer.preferred_auth_method == "captcha"


def test_rejects_bad_auth_method(client, db, lawyer):
    r = client.put(
        "/api/v1/credentials/me", headers=_h(LAWYER_RUT),
        json={"password": "x1234", "auth_method": "no_existe"},
    )
    assert r.status_code == 422


def test_requires_auth(client):
    r = client.put("/api/v1/credentials/me", json={"password": "x1234"})
    assert r.status_code == 401
