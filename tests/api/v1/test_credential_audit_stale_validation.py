"""Credential vault: a validation older than the last key change is stale.

Found on 2026-09-30 (lawyer 22, pjud): the vault showed "Fallando" although the
key had already been replaced 2.5 hours after the failure::

    15:37:05  value_changed      <- new key, already stored
    13:04:32  validation_failed  <- failure about the OLD key

Health looked only at the latest validation event, so it described a key that
no longer exists. A result older than the last change says nothing about the
current key: the honest state is ``pending_validation`` (not failing, and not
claimed valid either, because nobody has tried the new key against PJUD yet).
"""

from datetime import datetime, timedelta

import pytest

from app.models.credential_audit_event import CredentialAuditEvent
from app.models.lawyer import Lawyer
from app.services.credential_audit import credential_status, record_validation

T0 = datetime(2026, 9, 30, 13, 0, 0)


def _lawyer(db, *, ciphertext="ciphertext-A", alert_sent_at=None):
    lawyer = Lawyer(
        rut="66666666-6",
        name="Stale Validation Lawyer",
        role="lawyer",
        encrypted_pjud_password=ciphertext,
        preferred_auth_method="captcha",
    )
    lawyer.credential_alert_sent_at = alert_sent_at
    db.add(lawyer)
    db.commit()
    db.refresh(lawyer)
    return lawyer


def _event(db, lawyer, event_type, at, credential_type="pjud"):
    db.add(
        CredentialAuditEvent(
            lawyer_id=int(lawyer.id),
            credential_type=credential_type,
            event_type=event_type,
            occurred_at=at,
        )
    )
    db.commit()


def _pjud(db, lawyer):
    entry = next(s for s in credential_status(db) if s["lawyer_id"] == lawyer.id)
    return entry["pjud"]


def _hours(n):
    return T0 + timedelta(hours=n)


class TestValidationOlderThanTheChange:
    def test_failure_then_key_change_is_pending_not_failing(self, db):
        # The real case: the failure was about the old key.
        lawyer = _lawyer(db)
        _event(db, lawyer, "validation_failed", _hours(0))
        _event(db, lawyer, "value_changed", _hours(2))

        assert _pjud(db, lawyer)["health"] == "pending_validation"

    def test_ok_then_key_change_is_pending(self, db):
        # The old "ok" does not vouch for a key nobody has tested.
        lawyer = _lawyer(db)
        _event(db, lawyer, "validation_ok", _hours(0))
        _event(db, lawyer, "value_changed", _hours(2))

        assert _pjud(db, lawyer)["health"] == "pending_validation"

    def test_tied_timestamps_count_the_change_as_later(self, db):
        # Tie-break: "changed the key right after it failed" is the real case,
        # so an equal occurred_at means the change came AFTER the validation.
        lawyer = _lawyer(db)
        _event(db, lawyer, "validation_failed", _hours(0))
        _event(db, lawyer, "value_changed", _hours(0))

        assert _pjud(db, lawyer)["health"] == "pending_validation"


class TestValidationNewerThanTheChange:
    def test_key_change_then_failure_is_still_failing(self, db):
        # Regression guard: this failure DOES describe the current key.
        lawyer = _lawyer(db)
        _event(db, lawyer, "value_changed", _hours(0))
        _event(db, lawyer, "validation_failed", _hours(2))

        assert _pjud(db, lawyer)["health"] == "failing"

    def test_key_change_then_ok_is_valid(self, db):
        lawyer = _lawyer(db)
        _event(db, lawyer, "value_changed", _hours(0))
        _event(db, lawyer, "validation_ok", _hours(2))

        assert _pjud(db, lawyer)["health"] == "valid"


class TestNoCredentialPresent:
    @pytest.mark.parametrize("validation", ["validation_failed", "validation_ok"])
    @pytest.mark.parametrize("change_first", [True, False])
    def test_never_validated_whatever_the_events(self, db, validation, change_first):
        lawyer = _lawyer(db, ciphertext=None)
        if change_first:
            _event(db, lawyer, "value_changed", _hours(0))
            _event(db, lawyer, validation, _hours(2))
        else:
            _event(db, lawyer, validation, _hours(0))
            _event(db, lawyer, "value_changed", _hours(2))

        assert _pjud(db, lawyer)["health"] == "never_validated"


class TestAlertMarker:
    def test_alert_after_the_change_is_failing(self, db):
        lawyer = _lawyer(db, alert_sent_at=_hours(2))
        _event(db, lawyer, "value_changed", _hours(0))

        assert _pjud(db, lawyer)["health"] == "failing"

    def test_alert_before_the_change_is_pending(self, db):
        lawyer = _lawyer(db, alert_sent_at=_hours(0))
        _event(db, lawyer, "value_changed", _hours(2))

        assert _pjud(db, lawyer)["health"] == "pending_validation"

    def test_alert_tied_with_the_change_is_pending(self, db):
        lawyer = _lawyer(db, alert_sent_at=_hours(0))
        _event(db, lawyer, "value_changed", _hours(0))

        assert _pjud(db, lawyer)["health"] == "pending_validation"

    def test_alert_without_any_change_event_is_failing(self, db):
        lawyer = _lawyer(db, alert_sent_at=_hours(0))

        assert _pjud(db, lawyer)["health"] == "failing"


class TestHistoryIsStillReported:
    def test_timestamps_survive_a_pending_validation(self, db):
        lawyer = _lawyer(db)
        _event(db, lawyer, "validation_ok", _hours(0))
        _event(db, lawyer, "validation_failed", _hours(1))
        _event(db, lawyer, "value_changed", _hours(2))

        pjud = _pjud(db, lawyer)
        assert pjud["health"] == "pending_validation"
        assert pjud["last_validation_ok_at"] == _hours(0)
        assert pjud["last_failed_at"] == _hours(1)
        assert pjud["last_changed_at"] == _hours(2)

    def test_credential_types_are_independent(self, db):
        lawyer = _lawyer(db)
        lawyer.encrypted_clave_unica_password = "ciphertext-CU"
        db.commit()
        _event(db, lawyer, "validation_failed", _hours(0), "pjud")
        _event(db, lawyer, "value_changed", _hours(2), "pjud")
        _event(db, lawyer, "value_changed", _hours(0), "clave_unica")
        _event(db, lawyer, "validation_failed", _hours(2), "clave_unica")

        entry = next(s for s in credential_status(db) if s["lawyer_id"] == lawyer.id)
        assert entry["pjud"]["health"] == "pending_validation"
        assert entry["clave_unica"]["health"] == "failing"


class TestRecordValidationAfterAKeyChange:
    """Without this the pending state could never resolve.

    ``record_validation`` dedups by outcome. A lawyer whose last result was
    ``validation_ok`` and who then changed the key would get the next ``ok``
    swallowed as a duplicate, leaving the vault on ``pending_validation``
    forever (same for a new key that also fails).
    """

    def test_same_outcome_after_a_key_change_is_recorded(self, db):
        lawyer = _lawyer(db)
        _event(db, lawyer, "validation_failed", datetime.utcnow() - timedelta(hours=3))
        _event(db, lawyer, "value_changed", datetime.utcnow() - timedelta(hours=2))

        recorded = record_validation(db, int(lawyer.id), "pjud", ok=False, detail="invalid")

        assert recorded is not None
        assert _pjud(db, lawyer)["health"] == "failing"

    def test_ok_after_a_key_change_resolves_pending_to_valid(self, db):
        lawyer = _lawyer(db)
        _event(db, lawyer, "validation_ok", datetime.utcnow() - timedelta(hours=3))
        _event(db, lawyer, "value_changed", datetime.utcnow() - timedelta(hours=2))
        assert _pjud(db, lawyer)["health"] == "pending_validation"

        recorded = record_validation(db, int(lawyer.id), "pjud", ok=True)

        assert recorded is not None
        assert _pjud(db, lawyer)["health"] == "valid"

    def test_same_outcome_without_a_key_change_is_still_deduped(self, db):
        lawyer = _lawyer(db)
        _event(db, lawyer, "value_changed", datetime.utcnow() - timedelta(hours=3))
        _event(db, lawyer, "validation_failed", datetime.utcnow() - timedelta(hours=2))

        assert record_validation(db, int(lawyer.id), "pjud", ok=False) is None
