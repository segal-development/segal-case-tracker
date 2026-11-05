"""Migration 072: the cases identity key becomes (lawyer_id, rol, court_id, procedure).

The same ROL in two courts, or in two cuadernos of one court, are different
causas (measured 2026-10-21: 943 ROLs live in more than one court). The old
(lawyer_id, rol) constraint could not represent them. As in test_021, the
suite builds its schema from Base.metadata, so the enforcement tests run
against the model; the file tests only pin the revision chain.
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
    Path(__file__).parent.parent.parent / "alembic/versions/072_cases_identity_key.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_072", _MIGRATION_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration072File:
    def test_revision_chain(self):
        mod = _load_migration()
        assert mod.revision == "072"
        assert mod.down_revision == "071"

    def test_upgrade_and_downgrade_callable(self):
        mod = _load_migration()
        assert callable(mod.upgrade) and callable(mod.downgrade)


@pytest.fixture
def firm(db):
    lawyer = Lawyer(rut="16021492-9", name="Firm", is_active=True)
    db.add(lawyer)
    db.flush()
    return lawyer


def _court(db, code):
    court = Court(code=code, name=f"Juzgado {code}", region="RM", type="civil")
    db.add(court)
    db.flush()
    return court


def _case(lawyer, court, rol="C-1-2026", procedure="Principal"):
    return Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        procedure=procedure,
        competencia="civil",
        status="active",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )


class TestIdentityKey:
    def test_same_rol_in_two_courts_is_two_causas(self, db, firm):
        a, b = _court(db, "T72-A"), _court(db, "T72-B")
        db.add_all([_case(firm, a), _case(firm, b)])
        db.commit()  # no IntegrityError

    def test_same_rol_and_court_in_two_cuadernos_is_two_rows(self, db, firm):
        a = _court(db, "T72-C")
        db.add_all([_case(firm, a, procedure="Principal"), _case(firm, a, procedure="Medida prejudicial")])
        db.commit()  # no IntegrityError

    def test_same_rol_court_and_cuaderno_is_rejected(self, db, firm):
        a = _court(db, "T72-D")
        db.add(_case(firm, a))
        db.commit()
        db.add(_case(firm, a))
        with pytest.raises(IntegrityError):
            db.commit()

    def test_missing_cuaderno_does_not_defeat_the_key(self, db, firm):
        """NULL != NULL in a plain unique constraint; the key must still hold."""
        a = _court(db, "T72-E")
        db.add(_case(firm, a, procedure=None))
        db.commit()
        db.add(_case(firm, a, procedure=None))
        with pytest.raises(IntegrityError):
            db.commit()
