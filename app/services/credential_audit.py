"""Credential-monitoring service ("bóveda de credenciales") — read-only.

SECURITY — non-negotiable (this handles PJUD credentials / national identity):
- This module NEVER decrypts a credential and NEVER reads or returns plaintext.
- The only credential-derived artifact is ``sha256(ciphertext)`` — a hash of the
  ALREADY-ENCRYPTED blob string (``lawyer.encrypted_pjud_password`` /
  ``lawyer.encrypted_clave_unica_password``). Hashing ciphertext reveals nothing
  about the plaintext and is safe to store/compare.
- ``credential_status`` returns ONLY safe metadata: booleans, timestamps, and
  enum/status strings. It NEVER returns any credential value, ciphertext, or the
  internal fingerprint.
"""

import hashlib
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.credential_audit_event import CredentialAuditEvent
from app.models.lawyer import Lawyer

CREDENTIAL_TYPES = ("pjud", "clave_unica")

# Maps a credential type to the Lawyer column holding its encrypted blob.
_ENCRYPTED_FIELD = {
    "pjud": "encrypted_pjud_password",
    "clave_unica": "encrypted_clave_unica_password",
}

_VALIDATION_EVENTS = ("validation_ok", "validation_failed")


def _fingerprint(ciphertext: Optional[str]) -> Optional[str]:
    """Return ``sha256(ciphertext)`` hex, or None for an empty/absent value.

    Hashes the ALREADY-ENCRYPTED string — this never decrypts anything.
    """
    if not ciphertext:
        return None
    return hashlib.sha256(ciphertext.encode()).hexdigest()


def _latest_event(
    db: Session,
    lawyer_id: int,
    credential_type: str,
    event_types,
) -> Optional[CredentialAuditEvent]:
    """Most recent event (by occurred_at, then id) for a (lawyer, type) filter."""
    return (
        db.query(CredentialAuditEvent)
        .filter(CredentialAuditEvent.lawyer_id == lawyer_id)
        .filter(CredentialAuditEvent.credential_type == credential_type)
        .filter(CredentialAuditEvent.event_type.in_(list(event_types)))
        .order_by(
            CredentialAuditEvent.occurred_at.desc(),
            CredentialAuditEvent.id.desc(),
        )
        .first()
    )


def record_validation(
    db: Session,
    lawyer_id: int,
    credential_type: str,
    ok: bool,
    detail: Optional[str] = None,
) -> Optional[CredentialAuditEvent]:
    """Record a ``validation_ok``/``validation_failed`` event, deduped by outcome.

    Only inserts if the outcome DIFFERS from the most recent ``validation_*``
    event for this (lawyer_id, credential_type). Returns the new row, or None if
    the outcome was unchanged (deduped). Commits.
    """
    event_type = "validation_ok" if ok else "validation_failed"

    last = _latest_event(db, lawyer_id, credential_type, _VALIDATION_EVENTS)
    if last is not None and last.event_type == event_type:
        return None  # outcome unchanged — dedup

    event = CredentialAuditEvent(
        lawyer_id=lawyer_id,
        credential_type=credential_type,
        event_type=event_type,
        detail=detail,
        occurred_at=datetime.utcnow(),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def scan_credential_changes(db: Session) -> int:
    """Detect credential rotations without touching plaintext.

    For every lawyer and each credential type, compute the fingerprint of the
    stored ciphertext and compare it to the fingerprint of the most recent
    ``value_changed`` event for that (lawyer, type). On a difference (including
    the first time a credential is seen), insert a ``value_changed`` event with
    the new fingerprint. Returns the number of events recorded. Commits.
    """
    recorded = 0
    lawyers = db.query(Lawyer).all()

    for lawyer in lawyers:
        for credential_type in CREDENTIAL_TYPES:
            ciphertext = getattr(lawyer, _ENCRYPTED_FIELD[credential_type], None)
            current_fp = _fingerprint(ciphertext)

            last = _latest_event(db, int(lawyer.id), credential_type, ["value_changed"])
            last_fp = last.fingerprint if last is not None else None

            if current_fp == last_fp:
                continue  # no change (covers never-present -> still absent)

            db.add(
                CredentialAuditEvent(
                    lawyer_id=int(lawyer.id),
                    credential_type=credential_type,
                    event_type="value_changed",
                    fingerprint=current_fp,
                    occurred_at=datetime.utcnow(),
                )
            )
            recorded += 1

    if recorded:
        db.commit()
    return recorded


def _sub_status(db: Session, lawyer: Lawyer, credential_type: str) -> dict:
    """Build the safe per-credential-type status. No credential value leaks."""
    ciphertext = getattr(lawyer, _ENCRYPTED_FIELD[credential_type], None)
    present = bool(ciphertext)

    last_ok = _latest_event(db, int(lawyer.id), credential_type, ["validation_ok"])
    last_failed = _latest_event(db, int(lawyer.id), credential_type, ["validation_failed"])
    last_changed = _latest_event(db, int(lawyer.id), credential_type, ["value_changed"])
    latest_validation = _latest_event(db, int(lawyer.id), credential_type, _VALIDATION_EVENTS)

    if latest_validation is not None:
        health = "valid" if latest_validation.event_type == "validation_ok" else "failing"
    elif present and getattr(lawyer, "credential_alert_sent_at", None) is not None:
        # No recorded validation yet, but a failure-episode alert is active.
        health = "failing"
    else:
        health = "never_validated"

    return {
        "present": present,
        "health": health,
        "last_validation_ok_at": last_ok.occurred_at if last_ok else None,
        "last_failed_at": last_failed.occurred_at if last_failed else None,
        "last_changed_at": last_changed.occurred_at if last_changed else None,
    }


def credential_status(db: Session) -> list[dict]:
    """One safe-metadata entry per lawyer. NEVER exposes any credential value.

    Each entry contains only: identity fields, ``preferred_auth_method``, and a
    per-credential-type sub-status of booleans/timestamps/enum strings. The
    internal ciphertext fingerprint is deliberately NOT surfaced.
    """
    result: list[dict] = []
    for lawyer in db.query(Lawyer).order_by(Lawyer.id.asc()).all():
        result.append(
            {
                "lawyer_id": int(lawyer.id),
                "lawyer_name": lawyer.name,
                "lawyer_rut": lawyer.rut,
                "preferred_auth_method": lawyer.preferred_auth_method,
                "pjud": _sub_status(db, lawyer, "pjud"),
                "clave_unica": _sub_status(db, lawyer, "clave_unica"),
            }
        )
    return result
