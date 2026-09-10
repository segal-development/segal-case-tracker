"""Credential vault: inactive lawyers and removed credentials must not alert.

Found on 2026-09-10: a lawyer who left the firm (deactivated, credential
removed) kept showing as "failing" in the vault — and in the dashboard banner —
because health was derived from his last ``validation_failed`` event even
though no credential was stored anymore, and because the vault listed every
lawyer regardless of ``is_active``.
"""

from datetime import timedelta

import pytest

from app.core.security import create_access_token
from app.models.credential_audit_event import CredentialAuditEvent
from app.models.lawyer import Lawyer
from app.services.credential_audit import credential_status, record_validation, scan_credential_changes

AUDITOR_RUT = "44444444-4"


@pytest.fixture
def auditor(db):
    l = Lawyer(rut=AUDITOR_RUT, name="Auditor User", role="auditor")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def auditor_headers(auditor):
    tok = create_access_token({"sub": AUDITOR_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


def _lawyer(db, rut, *, active=True, ciphertext="ciphertext-A"):
    l = Lawyer(rut=rut, name=f"Lawyer {rut}", role="lawyer", is_active=active,
               encrypted_pjud_password=ciphertext, preferred_auth_method="captcha")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


def _by_rut(status, rut):
    return next((e for e in status if e["lawyer_rut"] == rut), None)


class TestInactiveLawyersLeaveTheVault:
    def test_inactive_lawyer_is_not_listed(self, db, auditor):
        _lawyer(db, "11111111-1", active=True)
        _lawyer(db, "22222222-2", active=False)
        ruts = {e["lawyer_rut"] for e in credential_status(db)}
        assert "11111111-1" in ruts
        assert "22222222-2" not in ruts

    def test_inactive_lawyer_with_failed_validation_does_not_alert(self, db, auditor):
        gone = _lawyer(db, "22222222-2", active=False, ciphertext=None)
        record_validation(db, int(gone.id), "pjud", ok=False, detail="credential_expired")
        assert all(e["pjud"]["health"] != "failing" for e in credential_status(db))

    def test_scan_skips_inactive_lawyers(self, db, auditor):
        gone = _lawyer(db, "22222222-2", active=False, ciphertext="ciphertext-Z")
        assert scan_credential_changes(db) == 0
        assert db.query(CredentialAuditEvent).filter(CredentialAuditEvent.lawyer_id == gone.id).count() == 0

    def test_status_endpoint_excludes_inactive(self, client, db, auditor_headers):
        _lawyer(db, "11111111-1", active=True)
        _lawyer(db, "22222222-2", active=False)
        r = client.get("/api/v1/credentials/status", headers=auditor_headers)
        assert r.status_code == 200
        ruts = {e["lawyer_rut"] for e in r.json()}
        assert "11111111-1" in ruts and "22222222-2" not in ruts


class TestRemovedCredentialIsNotFailing:
    def test_no_credential_means_never_validated_even_after_a_failure(self, db, auditor):
        """A credential that was removed cannot be 'failing': the vault must show
        'No cargada' (never_validated), keeping the timestamps for history."""
        l = _lawyer(db, "11111111-1", active=True, ciphertext="ciphertext-A")
        record_validation(db, int(l.id), "pjud", ok=False, detail="credential_expired")
        l.encrypted_pjud_password = None
        db.commit()
        s = _by_rut(credential_status(db), "11111111-1")["pjud"]
        assert s["present"] is False
        assert s["health"] == "never_validated"
        assert s["last_failed_at"] is not None  # history is kept

    def test_present_credential_with_failed_validation_still_alerts(self, db, auditor):
        l = _lawyer(db, "11111111-1", active=True, ciphertext="ciphertext-A")
        record_validation(db, int(l.id), "pjud", ok=False, detail="invalid_credentials")
        assert _by_rut(credential_status(db), "11111111-1")["pjud"]["health"] == "failing"
