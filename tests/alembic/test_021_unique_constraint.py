"""Tests for migration 021: uq_cases_lawyer_rol + ingest_keys table.

Two layers:
1. Migration file sanity (importable, correct revision chain) — matches the
   lightweight pattern used by other migration tests in this repo.
2. Actual constraint enforcement against the SQLAlchemy model, since the test
   suite builds its schema from ``Base.metadata`` (see tests/conftest.py),
   not by running Alembic migrations.
"""

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer

_MIGRATION_PATH = (
    Path(__file__).parent.parent.parent
    / "alembic/versions/021_ingest_keys_and_unique_rol.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_021", _MIGRATION_PATH)
    assert spec is not None, f"Migration file not found: {_MIGRATION_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration021Importable:
    def test_migration_file_exists(self):
        assert _MIGRATION_PATH.exists(), f"Expected migration at {_MIGRATION_PATH}"

    def test_upgrade_callable(self):
        mod = _load_migration()
        assert callable(mod.upgrade)

    def test_downgrade_callable(self):
        mod = _load_migration()
        assert callable(mod.downgrade)

    def test_revision_id(self):
        mod = _load_migration()
        assert mod.revision == "021"

    def test_down_revision_chains_to_020(self):
        mod = _load_migration()
        assert mod.down_revision == "020"


class TestUniqueConstraintEnforced:
    """The (lawyer_id, rol) pair must be unique at the model/DB level."""

    def test_duplicate_rol_for_same_lawyer_raises_integrity_error(self, db):
        lawyer = Lawyer(
            rut="11111111-1",
            name="Test Lawyer",
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(lawyer)
        db.flush()

        court = Court(code="T-UQ", name="Juzgado Test UQ", region="RM", type="civil")
        db.add(court)
        db.flush()

        db.add(
            Case(
                lawyer_id=lawyer.id,
                court_id=court.id,
                rol="C-1234-2026",
                competencia="civil",
                status="active",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        db.commit()

        db.add(
            Case(
                lawyer_id=lawyer.id,
                court_id=court.id,
                rol="C-1234-2026",
                competencia="civil",
                status="active",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    def test_same_rol_for_different_lawyers_is_allowed(self, db):
        lawyer_a = Lawyer(
            rut="11111111-1",
            name="Lawyer A",
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        lawyer_b = Lawyer(
            rut="22222222-2",
            name="Lawyer B",
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add_all([lawyer_a, lawyer_b])
        db.flush()

        court = Court(code="T-UQ2", name="Juzgado Test UQ2", region="RM", type="civil")
        db.add(court)
        db.flush()

        db.add_all(
            [
                Case(
                    lawyer_id=lawyer_a.id,
                    court_id=court.id,
                    rol="C-5555-2026",
                    competencia="civil",
                    status="active",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                ),
                Case(
                    lawyer_id=lawyer_b.id,
                    court_id=court.id,
                    rol="C-5555-2026",
                    competencia="civil",
                    status="active",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                ),
            ]
        )
        db.commit()  # must not raise
