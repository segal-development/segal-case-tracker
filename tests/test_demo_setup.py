"""Tests for demo_setup script core logic — STRICT TDD."""

import os

os.environ.setdefault("ENVIRONMENT", "development")

from scripts.demo_setup import ensure_demo_lawyer, mint_token  # noqa: E402
from app.core.security import decode_access_token  # noqa: E402
from app.models.lawyer import Lawyer  # noqa: E402


class TestEnsureDemoLawyer:
    def test_creates_lawyer_when_not_exists(self, db):
        """Creates a Lawyer with the given rut+email when none exists; id is a non-None int."""
        lawyer = ensure_demo_lawyer(db, rut="16021492-9", email="demo@segal.cl", name="Demo Lawyer")

        assert lawyer.id is not None
        assert isinstance(lawyer.id, int)
        assert lawyer.rut == "16021492-9"
        assert lawyer.email == "demo@segal.cl"
        assert lawyer.name == "Demo Lawyer"

    def test_idempotent_does_not_duplicate(self, db):
        """Calling twice with the same rut creates exactly one Lawyer row."""
        ensure_demo_lawyer(db, rut="16021492-9", email="demo@segal.cl")
        ensure_demo_lawyer(db, rut="16021492-9", email="demo@segal.cl")

        count = db.query(Lawyer).filter(Lawyer.rut == "16021492-9").count()
        assert count == 1

    def test_updates_email_on_second_call(self, db):
        """Updates email when the same rut is called again with a different email."""
        ensure_demo_lawyer(db, rut="16021492-9", email="old@segal.cl")
        lawyer = ensure_demo_lawyer(db, rut="16021492-9", email="new@segal.cl")

        assert lawyer.email == "new@segal.cl"

        # Still only one row
        count = db.query(Lawyer).filter(Lawyer.rut == "16021492-9").count()
        assert count == 1


class TestMintToken:
    def test_returns_decodable_jwt(self, db):
        """mint_token returns a JWT that decode_access_token can verify."""
        lawyer = ensure_demo_lawyer(db, rut="16021492-9", email="demo@segal.cl")
        token = mint_token(lawyer.id)

        payload = decode_access_token(token)
        assert payload is not None

    def test_sub_equals_str_of_lawyer_id(self, db):
        """payload['sub'] is str(lawyer.id) and parses back to lawyer.id as int."""
        lawyer = ensure_demo_lawyer(db, rut="16021492-9", email="demo@segal.cl")
        token = mint_token(lawyer.id)

        payload = decode_access_token(token)
        assert payload["sub"] == str(lawyer.id)
        assert int(payload["sub"]) == lawyer.id
