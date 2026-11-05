"""Migration 073: backfill case_lawyer_source from Case.lawyer_id.

The suite builds its schema from Base.metadata, so the backfill SQL is executed
against the SQLite test session; the file tests pin the revision chain.
"""

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.court import Court
from app.models.lawyer import Lawyer

_MIGRATION_PATH = (
    Path(__file__).parent.parent.parent
    / "alembic/versions/073_backfill_case_lawyer_source.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_073", _MIGRATION_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _backfill(db):
    db.execute(text(_load_migration().BACKFILL_SQL))
    db.commit()


class TestMigration073File:
    def test_revision_chain(self):
        mod = _load_migration()
        assert mod.revision == "073"
        assert mod.down_revision == "072"

    def test_upgrade_and_downgrade_callable(self):
        mod = _load_migration()
        assert callable(mod.upgrade) and callable(mod.downgrade)


@pytest.fixture
def court(db):
    c = Court(code="T73", name="Juzgado T73", region="RM", type="civil")
    db.add(c)
    db.flush()
    return c


def _lawyer(db, rut):
    lawyer = Lawyer(rut=rut, name=f"L {rut}", is_active=True)
    db.add(lawyer)
    db.flush()
    return lawyer


def _case(db, lawyer, court, rol, created_at=None):
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        procedure="Principal",
        competencia="civil",
        status="active",
        created_at=created_at,
        updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.flush()
    return case


def _rows(db):
    return db.query(CaseLawyerSource).order_by(CaseLawyerSource.case_id).all()


class TestBackfill:
    def test_one_row_per_case_with_owner_and_created_at(self, db, court):
        lawyer = _lawyer(db, "11111111-1")
        created = datetime(2024, 5, 1, 10, 0, 0)
        a = _case(db, lawyer, court, "C-1-2026", created_at=created)
        b = _case(db, lawyer, court, "C-2-2026", created_at=created)
        db.commit()

        _backfill(db)

        rows = _rows(db)
        assert [(r.case_id, r.lawyer_id) for r in rows] == [
            (a.id, lawyer.id),
            (b.id, lawyer.id),
        ]
        assert all(r.first_seen_at == created and r.last_seen_at == created for r in rows)

    def test_null_created_at_falls_back_to_now(self, db, court):
        lawyer = _lawyer(db, "22222222-2")
        _case(db, lawyer, court, "C-3-2026", created_at=None)
        db.commit()
        before = datetime.utcnow()

        _backfill(db)

        (row,) = _rows(db)
        assert row.first_seen_at is not None and row.last_seen_at is not None

    def test_idempotent(self, db, court):
        lawyer = _lawyer(db, "33333333-3")
        _case(db, lawyer, court, "C-4-2026", created_at=datetime(2024, 1, 1))
        db.commit()

        _backfill(db)
        _backfill(db)

        assert len(_rows(db)) == 1

    def test_existing_rows_are_untouched(self, db, court):
        lawyer = _lawyer(db, "44444444-4")
        case = _case(db, lawyer, court, "C-5-2026", created_at=datetime(2024, 1, 1))
        seen = datetime(2026, 9, 9, 9, 9, 9)
        db.add(
            CaseLawyerSource(
                case_id=case.id, lawyer_id=lawyer.id, first_seen_at=seen, last_seen_at=seen
            )
        )
        db.commit()

        _backfill(db)
        db.expire_all()

        (row,) = _rows(db)
        assert row.first_seen_at == seen and row.last_seen_at == seen

    def test_other_lawyer_source_does_not_block_owner_row(self, db, court):
        owner = _lawyer(db, "55555555-5")
        other = _lawyer(db, "66666666-6")
        case = _case(db, owner, court, "C-6-2026", created_at=datetime(2024, 1, 1))
        db.add(CaseLawyerSource(case_id=case.id, lawyer_id=other.id))
        db.commit()

        _backfill(db)

        assert {(r.case_id, r.lawyer_id) for r in _rows(db)} == {
            (case.id, owner.id),
            (case.id, other.id),
        }

    def test_each_lawyer_gets_their_own_rows(self, db, court):
        l1, l2 = _lawyer(db, "77777777-7"), _lawyer(db, "88888888-8")
        c1 = _case(db, l1, court, "C-7-2026", created_at=datetime(2024, 1, 1))
        c2 = _case(db, l2, court, "C-7-2026", created_at=datetime(2024, 1, 1))
        db.commit()

        _backfill(db)

        assert {(r.case_id, r.lawyer_id) for r in _rows(db)} == {
            (c1.id, l1.id),
            (c2.id, l2.id),
        }
