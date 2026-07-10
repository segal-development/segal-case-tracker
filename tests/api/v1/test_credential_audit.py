"""Tests for the read-only credential-monitoring module ("bóveda de credenciales").

SECURITY CONTRACT under test:
- No plaintext or ciphertext credential value is EVER returned by the status
  endpoint. The only credential-derived artifact is ``sha256(ciphertext)`` — a
  hash of the ALREADY-ENCRYPTED blob — which stays internal to the audit table
  and is never surfaced in responses.
"""

import hashlib
from datetime import timedelta

import pytest

from app.core.security import create_access_token
from app.models.lawyer import Lawyer
from app.models.credential_audit_event import CredentialAuditEvent
from app.services.credential_audit import (
    _fingerprint,
    record_validation,
    scan_credential_changes,
    credential_status,
)

AUDITOR_RUT = "44444444-4"
LAWYER_RUT = "55555555-5"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auditor(db):
    l = Lawyer(rut=AUDITOR_RUT, name="Auditor User", role="auditor")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def monitored_lawyer(db):
    l = Lawyer(
        rut=LAWYER_RUT,
        name="Monitored Lawyer",
        role="lawyer",
        encrypted_pjud_password="ciphertext-A",
        preferred_auth_method="captcha",
    )
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def auditor_headers(auditor):
    tok = create_access_token({"sub": AUDITOR_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def lawyer_headers(monitored_lawyer):
    tok = create_access_token({"sub": LAWYER_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
# _fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_sha256_hex():
    fp = _fingerprint("ciphertext-A")
    assert fp == hashlib.sha256(b"ciphertext-A").hexdigest()
    assert fp == _fingerprint("ciphertext-A")  # stable
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_differs_for_different_ciphertext():
    assert _fingerprint("ciphertext-A") != _fingerprint("ciphertext-B")


def test_fingerprint_none_for_empty():
    assert _fingerprint(None) is None
    assert _fingerprint("") is None


# ---------------------------------------------------------------------------
# record_validation — dedup by outcome
# ---------------------------------------------------------------------------


def test_record_validation_dedups_consecutive_same_outcome(db, monitored_lawyer):
    first = record_validation(db, monitored_lawyer.id, "pjud", ok=True)
    second = record_validation(db, monitored_lawyer.id, "pjud", ok=True)

    assert first is not None
    assert second is None  # deduped: outcome did not change
    rows = (
        db.query(CredentialAuditEvent)
        .filter(CredentialAuditEvent.lawyer_id == monitored_lawyer.id)
        .filter(CredentialAuditEvent.event_type.in_(["validation_ok", "validation_failed"]))
        .all()
    )
    assert len(rows) == 1
    assert rows[0].event_type == "validation_ok"


def test_record_validation_records_on_outcome_change(db, monitored_lawyer):
    ok = record_validation(db, monitored_lawyer.id, "pjud", ok=True)
    failed = record_validation(
        db, monitored_lawyer.id, "pjud", ok=False, detail="invalid_credentials"
    )

    assert ok is not None
    assert failed is not None
    assert failed.event_type == "validation_failed"
    assert failed.detail == "invalid_credentials"
    rows = (
        db.query(CredentialAuditEvent)
        .filter(CredentialAuditEvent.lawyer_id == monitored_lawyer.id)
        .filter(CredentialAuditEvent.event_type.in_(["validation_ok", "validation_failed"]))
        .all()
    )
    assert len(rows) == 2


def test_record_validation_is_scoped_per_credential_type(db, monitored_lawyer):
    a = record_validation(db, monitored_lawyer.id, "pjud", ok=True)
    b = record_validation(db, monitored_lawyer.id, "clave_unica", ok=True)
    # Different credential types keep independent dedup state.
    assert a is not None
    assert b is not None


# ---------------------------------------------------------------------------
# scan_credential_changes — change detection over ciphertext hash
# ---------------------------------------------------------------------------


def test_scan_records_value_changed_then_dedups_same_value(db, monitored_lawyer):
    # First scan: credential present -> one value_changed
    recorded = scan_credential_changes(db)
    assert recorded == 1

    changed = (
        db.query(CredentialAuditEvent)
        .filter(CredentialAuditEvent.lawyer_id == monitored_lawyer.id)
        .filter(CredentialAuditEvent.event_type == "value_changed")
        .all()
    )
    assert len(changed) == 1
    assert changed[0].credential_type == "pjud"
    assert changed[0].fingerprint == _fingerprint("ciphertext-A")

    # Second scan: same value -> nothing new
    assert scan_credential_changes(db) == 0
    changed = (
        db.query(CredentialAuditEvent)
        .filter(CredentialAuditEvent.event_type == "value_changed")
        .all()
    )
    assert len(changed) == 1


def test_scan_records_new_event_when_value_rotates(db, monitored_lawyer):
    scan_credential_changes(db)  # baseline event for ciphertext-A

    monitored_lawyer.encrypted_pjud_password = "ciphertext-B"
    db.commit()

    assert scan_credential_changes(db) == 1
    changed = (
        db.query(CredentialAuditEvent)
        .filter(CredentialAuditEvent.event_type == "value_changed")
        .order_by(CredentialAuditEvent.occurred_at.asc(), CredentialAuditEvent.id.asc())
        .all()
    )
    assert len(changed) == 2
    assert changed[-1].fingerprint == _fingerprint("ciphertext-B")


# ---------------------------------------------------------------------------
# credential_status — safe metadata shape
# ---------------------------------------------------------------------------


def test_credential_status_reports_safe_metadata(db, monitored_lawyer):
    scan_credential_changes(db)
    record_validation(db, monitored_lawyer.id, "pjud", ok=True)

    statuses = credential_status(db)
    entry = next(s for s in statuses if s["lawyer_id"] == monitored_lawyer.id)

    assert entry["lawyer_name"] == "Monitored Lawyer"
    assert entry["lawyer_rut"] == LAWYER_RUT
    assert entry["pjud"]["present"] is True
    assert entry["pjud"]["health"] == "valid"
    assert entry["pjud"]["last_validation_ok_at"] is not None
    assert entry["pjud"]["last_changed_at"] is not None
    assert entry["clave_unica"]["present"] is False
    assert entry["clave_unica"]["health"] == "never_validated"


# ---------------------------------------------------------------------------
# Endpoint — GET /credentials/status
# ---------------------------------------------------------------------------


def _assert_no_credential_leak(payload):
    """No ciphertext/plaintext/fingerprint anywhere in the serialized response."""
    blob = str(payload).lower()
    assert "ciphertext-a" not in blob
    assert "ciphertext-b" not in blob
    assert "password" not in blob
    assert "fingerprint" not in blob
    assert "encrypted" not in blob


def test_status_endpoint_returns_safe_entries_for_auditor(
    client, db, monitored_lawyer, auditor, auditor_headers
):
    scan_credential_changes(db)
    record_validation(db, monitored_lawyer.id, "pjud", ok=True)

    resp = client.get("/api/v1/credentials/status", headers=auditor_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    entry = next(s for s in data if s["lawyer_id"] == monitored_lawyer.id)
    assert entry["pjud"]["present"] is True
    assert entry["pjud"]["health"] == "valid"
    assert set(entry["pjud"].keys()) == {
        "present",
        "health",
        "last_validation_ok_at",
        "last_failed_at",
        "last_changed_at",
    }
    _assert_no_credential_leak(data)


def test_status_endpoint_forbidden_for_non_auditor(client, monitored_lawyer, lawyer_headers):
    resp = client.get("/api/v1/credentials/status", headers=lawyer_headers)
    assert resp.status_code == 403


def test_scan_endpoint_forbidden_for_non_auditor(client, monitored_lawyer, lawyer_headers):
    # The mutating endpoint must be no weaker than the read one — lock it in so a
    # future edit can't silently drop require_auditor.
    resp = client.post("/api/v1/credentials/scan", headers=lawyer_headers)
    assert resp.status_code == 403


def test_scan_endpoint_triggers_change_detection_for_auditor(
    client, db, monitored_lawyer, auditor, auditor_headers
):
    resp = client.post("/api/v1/credentials/scan", headers=auditor_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recorded"] == 1
