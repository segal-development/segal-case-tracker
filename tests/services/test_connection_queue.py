"""Tests for DB-backed connection_queue.py.

TDD RED → GREEN (migrated from Redis to Cloud SQL).
Uses the shared ``db`` fixture from conftest.py (SQLite in-memory).

Security invariant: pending_connections MUST NOT have any password-bearing
column (INV-1 / INV-4).
"""

import pytest
from datetime import datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lawyer(db, rut: str = "12345678-9", name: str = "Test Lawyer"):
    from app.models.lawyer import Lawyer

    lawyer = Lawyer(rut=rut, name=name)
    db.add(lawyer)
    db.flush()
    return lawyer


# ---------------------------------------------------------------------------
# enqueue_connection
# ---------------------------------------------------------------------------


class TestEnqueueConnection:
    """enqueue_connection inserts a pending row and returns a UUID."""

    def test_enqueue_returns_uuid_connection_id(self, db):
        """enqueue_connection returns a non-empty UUID4 string (36 chars)."""
        from app.services.connection_queue import enqueue_connection

        _make_lawyer(db)
        cid = enqueue_connection(
            db,
            lawyer_id=1,
            rut="12345678-9",
            auth_method="segunda_clave",
            captcha_token="tok123",
        )
        assert isinstance(cid, str)
        assert len(cid) == 36  # UUID4 "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

    def test_enqueue_inserts_pending_row(self, db):
        """enqueue_connection creates a DB row with status='pending'."""
        from app.services.connection_queue import enqueue_connection
        from app.models.pending_connection import PendingConnection

        lawyer = _make_lawyer(db)
        cid = enqueue_connection(
            db, lawyer_id=lawyer.id, rut="12345678-9", auth_method="segunda_clave"
        )
        row = db.query(PendingConnection).filter_by(connection_id=cid).first()
        assert row is not None
        assert row.status == "pending"
        assert row.picked_at is None


# ---------------------------------------------------------------------------
# dequeue_connection
# ---------------------------------------------------------------------------


class TestDequeueConnection:
    """dequeue_connection claims the oldest pending row atomically."""

    def test_dequeue_returns_full_job_dict(self, db):
        """dequeue_connection returns the job dict with expected keys."""
        from app.services.connection_queue import enqueue_connection, dequeue_connection

        lawyer = _make_lawyer(db)
        cid = enqueue_connection(
            db,
            lawyer_id=lawyer.id,
            rut="12345678-9",
            auth_method="segunda_clave",
            captcha_token="abc",
        )
        job = dequeue_connection(db)

        assert job is not None
        assert job["connection_id"] == cid
        assert job["lawyer_id"] == lawyer.id
        assert job["rut"] == "12345678-9"
        assert job["auth_method"] == "segunda_clave"
        assert job["captcha_token"] == "abc"

    def test_dequeue_transitions_status_to_connecting(self, db):
        """dequeue_connection sets status='connecting' and stamps picked_at."""
        from app.services.connection_queue import enqueue_connection, dequeue_connection
        from app.models.pending_connection import PendingConnection

        lawyer = _make_lawyer(db)
        cid = enqueue_connection(db, lawyer_id=lawyer.id, rut="1-1", auth_method="segunda_clave")
        dequeue_connection(db)

        row = db.query(PendingConnection).filter_by(connection_id=cid).first()
        assert row.status == "connecting"
        assert row.picked_at is not None

    def test_dequeue_empty_returns_none(self, db):
        """dequeue_connection returns None when there are no pending rows."""
        from app.services.connection_queue import dequeue_connection

        assert dequeue_connection(db) is None

    def test_dequeue_fifo_order(self, db):
        """The oldest pending job is returned first."""
        import time
        from app.services.connection_queue import enqueue_connection, dequeue_connection

        lawyer = _make_lawyer(db)
        cid1 = enqueue_connection(db, lawyer_id=lawyer.id, rut="1-1", auth_method="segunda_clave")
        time.sleep(0.01)
        enqueue_connection(db, lawyer_id=lawyer.id, rut="2-2", auth_method="segunda_clave")

        job = dequeue_connection(db)
        assert job["connection_id"] == cid1

    def test_dequeue_skips_already_connecting_row(self, db):
        """A row with status='connecting' is not re-dequeued."""
        from app.services.connection_queue import enqueue_connection, dequeue_connection

        lawyer = _make_lawyer(db)
        enqueue_connection(db, lawyer_id=lawyer.id, rut="1-1", auth_method="segunda_clave")
        # First dequeue claims it
        job1 = dequeue_connection(db)
        assert job1 is not None
        # Second dequeue finds nothing (the row is now 'connecting')
        job2 = dequeue_connection(db)
        assert job2 is None


# ---------------------------------------------------------------------------
# set_status / get_status
# ---------------------------------------------------------------------------


class TestConnectionStatus:
    """set_status / get_status round-trip through the DB."""

    def test_set_and_get_status_connected(self, db):
        """set_status + get_status round-trip with connected status."""
        from app.services.connection_queue import enqueue_connection, dequeue_connection, set_status, get_status

        lawyer = _make_lawyer(db)
        cid = enqueue_connection(db, lawyer_id=lawyer.id, rut="1-1", auth_method="segunda_clave")
        dequeue_connection(db)
        set_status(db, cid, status="connected", session_id="sess-xyz", cases_synced=42)

        result = get_status(db, cid)
        assert result is not None
        assert result["status"] == "connected"
        assert result["session_id"] == "sess-xyz"
        assert result["cases_synced"] == 42

    def test_set_and_get_status_failed(self, db):
        """set_status + get_status round-trip with failed status."""
        from app.services.connection_queue import enqueue_connection, dequeue_connection, set_status, get_status

        lawyer = _make_lawyer(db)
        cid = enqueue_connection(db, lawyer_id=lawyer.id, rut="1-1", auth_method="segunda_clave")
        dequeue_connection(db)
        set_status(db, cid, status="failed", error="token_expired")

        result = get_status(db, cid)
        assert result["status"] == "failed"
        assert result["error"] == "token_expired"

    def test_get_status_unknown_connection_returns_none(self, db):
        """get_status returns None for an unknown connection_id."""
        from app.services.connection_queue import get_status

        assert get_status(db, "nonexistent-id-xyz") is None


# ---------------------------------------------------------------------------
# Security invariants
# ---------------------------------------------------------------------------


class TestSecurityInvariants:
    """INV-1 / INV-4: no credential column may appear in pending_connections."""

    def test_model_has_no_password_column(self, db):
        """The pending_connections table MUST NOT have any password-bearing column."""
        from app.models.pending_connection import PendingConnection

        forbidden = {
            "password",
            "encrypted_password",
            "pwd",
            "secret",
            "plaintext",
            "encrypted_pjud_password",
            "encrypted_cu_password",
            "encrypted_clave_unica_password",
        }
        col_names = {c.name for c in PendingConnection.__table__.columns}
        found = forbidden & col_names
        assert not found, f"Credential column(s) {found!r} found in pending_connections"

    def test_captcha_token_is_not_a_password(self, db):
        """captcha_token stores only the reCAPTCHA token, never a PJUD password."""
        from app.services.connection_queue import enqueue_connection
        from app.models.pending_connection import PendingConnection

        lawyer = _make_lawyer(db)
        CAPTCHA = "recaptcha-v3-token-abc"
        cid = enqueue_connection(
            db,
            lawyer_id=lawyer.id,
            rut="1-1",
            auth_method="segunda_clave",
            captcha_token=CAPTCHA,
        )
        row = db.query(PendingConnection).filter_by(connection_id=cid).first()
        # The column exists and holds the captcha token (not a password)
        assert row.captcha_token == CAPTCHA
