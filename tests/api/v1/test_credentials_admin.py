"""An administrator loads or changes ANOTHER lawyer's key: PUT /credentials/lawyers/{id}/{system}.

Security contract under test: admin only (an auditor is read-only), the key is stored
encrypted and never echoed or logged, the lawyer's preferred auth method is not flipped
by loading a secondary slot, and an audit event records who did it.
"""

import logging
from datetime import datetime

import pytest

from app.core.security import create_access_token, decrypt_pjud_password, encrypt_pjud_password
from app.models.credential_audit_event import CredentialAuditEvent
from app.models.lawyer import Lawyer
from app.services.credential_audit import scan_credential_changes

ADMIN_RUT = "11111111-1"
AUDITOR_RUT = "22222222-2"
OWN_RUT = "33333333-3"
TARGET_RUT = "44444444-4"
SECRET = "claveSuperSecreta-9271"
OLD_KEY = "claveVieja-1234"


def _lawyer(db, rut, role, name, **kw):
    obj = Lawyer(rut=rut, name=name, email=f"{rut}@segal.cl", role=role, is_active=True, **kw)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def people(db):
    return {
        "admin": _lawyer(db, ADMIN_RUT, "admin", "Admin Uno"),
        "auditor": _lawyer(db, AUDITOR_RUT, "auditor", "Auditor Dos"),
        "own": _lawyer(db, OWN_RUT, "lawyer", "Abogada Tres"),
        "target": _lawyer(
            db, TARGET_RUT, "lawyer", "Abogado Cuatro",
            preferred_auth_method="captcha", credential_alert_sent_at=datetime.utcnow(),
            encrypted_pjud_password=encrypt_pjud_password(OLD_KEY),
        ),
    }


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


def _put(client, lawyer_id, system, password=SECRET, rut=ADMIN_RUT):
    return client.put(
        f"/api/v1/credentials/lawyers/{lawyer_id}/{system}",
        headers=_h(rut), json={"password": password},
    )


def test_admin_loads_the_pjud_key_of_another_lawyer(client, db, people):
    target = people["target"]

    r = _put(client, target.id, "pjud")

    assert r.status_code == 200
    assert r.json() == {"ok": True, "system": "pjud"}
    assert SECRET not in r.text
    db.refresh(target)
    assert target.encrypted_pjud_password != SECRET
    assert decrypt_pjud_password(target.encrypted_pjud_password) == SECRET


def test_admin_loads_clave_unica_with_the_lawyers_own_rut(client, db, people):
    target = people["target"]

    r = _put(client, target.id, "clave_unica")

    assert r.status_code == 200
    db.refresh(target)
    assert decrypt_pjud_password(target.encrypted_clave_unica_password) == SECRET
    assert target.clave_unica_rut == TARGET_RUT


def test_loading_a_secondary_slot_does_not_flip_the_preferred_method(client, db, people):
    """The worker re-authenticates through preferred_auth_method: an admin loading the
    OTHER slot must not silently change how that lawyer logs in."""
    target = people["target"]  # prefers captcha and HAS a captcha key

    _put(client, target.id, "clave_unica")

    db.refresh(target)
    assert target.preferred_auth_method == "captcha"


@pytest.mark.parametrize("system,expected", [("pjud", "captcha"), ("clave_unica", "clave_unica")])
def test_preferred_method_follows_the_loaded_key_when_its_own_slot_is_empty(client, db, people, system, expected):
    """A lawyer with no key at all (the column defaults to captcha) gets the method of the
    key just loaded, otherwise the worker would try an empty slot and skip them."""
    own = people["own"]
    assert own.encrypted_pjud_password is None and own.encrypted_clave_unica_password is None

    _put(client, own.id, system)

    db.refresh(own)
    assert own.preferred_auth_method == expected


def test_loading_the_preferred_slot_itself_keeps_the_method(client, db, people):
    target = people["target"]

    _put(client, target.id, "pjud")

    db.refresh(target)
    assert target.preferred_auth_method == "captcha"


def test_resets_the_alert_marker_so_a_future_failure_alerts_again(client, db, people):
    target = people["target"]
    assert target.credential_alert_sent_at is not None

    _put(client, target.id, "pjud")

    db.refresh(target)
    assert target.credential_alert_sent_at is None


def test_records_who_loaded_it_without_any_credential_value(client, db, people):
    target, admin = people["target"], people["admin"]

    _put(client, target.id, "pjud")

    events = db.query(CredentialAuditEvent).filter_by(lawyer_id=target.id, credential_type="pjud").all()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "value_changed"
    assert ev.fingerprint and len(ev.fingerprint) == 64
    assert str(admin.id) in (ev.detail or "")
    assert SECRET not in (ev.detail or "") and SECRET not in ev.fingerprint


def test_the_change_scanner_does_not_record_the_same_change_twice(client, db, people):
    target = people["target"]
    _put(client, target.id, "pjud")

    scan_credential_changes(db)

    pjud_events = db.query(CredentialAuditEvent).filter_by(
        lawyer_id=target.id, credential_type="pjud", event_type="value_changed"
    ).count()
    assert pjud_events == 1


def test_the_key_is_never_logged(client, db, people, caplog):
    with caplog.at_level(logging.DEBUG):
        _put(client, people["target"].id, "pjud")

    assert SECRET not in caplog.text


@pytest.mark.parametrize("rut", [AUDITOR_RUT, OWN_RUT])
def test_only_an_admin_may_do_it(client, db, people, rut):
    r = _put(client, people["target"].id, "pjud", rut=rut)

    assert r.status_code == 403
    db.refresh(people["target"])
    assert decrypt_pjud_password(people["target"].encrypted_pjud_password) == OLD_KEY


def test_requires_authentication(client, people):
    r = client.put(f"/api/v1/credentials/lawyers/{people['target'].id}/pjud", json={"password": SECRET})
    assert r.status_code in (401, 403)


def test_unknown_lawyer_is_404(client, people):
    assert _put(client, 999999, "pjud").status_code == 404


def test_unknown_system_is_rejected(client, people):
    assert _put(client, people["target"].id, "otra").status_code == 422


def test_a_too_short_key_is_rejected_and_nothing_is_stored(client, db, people):
    r = _put(client, people["target"].id, "pjud", password="abc")

    assert r.status_code == 422
    db.refresh(people["target"])
    assert decrypt_pjud_password(people["target"].encrypted_pjud_password) == OLD_KEY


def test_the_self_service_endpoint_is_unchanged(client, db, people):
    own = people["own"]

    r = client.put(
        "/api/v1/credentials/me", headers=_h(OWN_RUT),
        json={"password": "propiaClave123", "auth_method": "clave_unica"},
    )

    assert r.status_code == 200
    db.refresh(own)
    assert own.preferred_auth_method == "clave_unica"
