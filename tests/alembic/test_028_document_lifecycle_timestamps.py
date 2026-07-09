"""Tests for migration 028: Document.stored_at / Document.failed_at + backfill.

Two layers, matching the lightweight pattern used by other migration tests
in this repo (see tests/alembic/test_021_unique_constraint.py):
1. Migration file sanity (importable, correct revision chain).
2. The backfill data step (`_backfill_stored_at`) exercised directly against
   the test DB bind — the test suite builds its schema from
   ``Base.metadata`` (see tests/conftest.py), not by running Alembic
   migrations, so calling the full `upgrade()` (which re-adds columns the
   model already defines) would conflict with the existing test schema.
"""

import importlib.util
from datetime import datetime
from pathlib import Path

from app.models.case import Case
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer

_MIGRATION_PATH = (
    Path(__file__).parent.parent.parent
    / "alembic/versions/028_add_document_lifecycle_timestamps.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_028", _MIGRATION_PATH)
    assert spec is not None, f"Migration file not found: {_MIGRATION_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration028Importable:
    def test_migration_file_exists(self):
        assert _MIGRATION_PATH.exists()

    def test_upgrade_and_downgrade_callable(self):
        mod = _load_migration()
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)

    def test_revision_chain(self):
        mod = _load_migration()
        assert mod.revision == "028"
        assert mod.down_revision == "027"


class TestBackfillStoredAt:
    """`_backfill_stored_at` must set stored_at = downloaded_at for existing
    stored docs missing it, and leave everything else untouched."""

    def _make_case(self, db):
        lawyer = Lawyer(rut="19191919-1", name="Migration028 Lawyer")
        db.add(lawyer)
        db.flush()
        court = Court(code="T-MIG028", name="Juzgado Migration028", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(
            lawyer_id=lawyer.id,
            court_id=court.id,
            rol="C-MIG-028",
            competencia="civil",
            status="active",
        )
        db.add(case)
        db.commit()
        db.refresh(case)
        return case

    def test_backfills_stored_docs_missing_stored_at(self, db):
        case = self._make_case(db)
        downloaded = datetime(2026, 6, 1, 12, 0, 0)
        stored_missing = Document(
            case_id=case.id,
            doc_type="resolution",
            status="stored",
            downloaded_at=downloaded,
            stored_at=None,
        )
        db.add(stored_missing)
        db.commit()
        db.refresh(stored_missing)

        mod = _load_migration()
        mod._backfill_stored_at(db.connection())
        db.commit()
        db.refresh(stored_missing)

        assert stored_missing.stored_at == downloaded

    def test_does_not_overwrite_existing_stored_at(self, db):
        case = self._make_case(db)
        already_set = datetime(2026, 6, 2, 9, 0, 0)
        stored_with_ts = Document(
            case_id=case.id,
            doc_type="resolution",
            status="stored",
            downloaded_at=datetime(2026, 6, 1, 12, 0, 0),
            stored_at=already_set,
        )
        db.add(stored_with_ts)
        db.commit()
        db.refresh(stored_with_ts)

        mod = _load_migration()
        mod._backfill_stored_at(db.connection())
        db.commit()
        db.refresh(stored_with_ts)

        assert stored_with_ts.stored_at == already_set

    def test_does_not_touch_non_stored_docs(self, db):
        case = self._make_case(db)
        pending = Document(
            case_id=case.id,
            doc_type="resolution",
            status="pending",
            downloaded_at=datetime(2026, 6, 1, 12, 0, 0),
            stored_at=None,
        )
        failed = Document(
            case_id=case.id,
            doc_type="resolution",
            status="failed",
            downloaded_at=datetime(2026, 6, 1, 12, 0, 0),
            stored_at=None,
        )
        db.add_all([pending, failed])
        db.commit()
        db.refresh(pending)
        db.refresh(failed)

        mod = _load_migration()
        mod._backfill_stored_at(db.connection())
        db.commit()
        db.refresh(pending)
        db.refresh(failed)

        assert pending.stored_at is None
        assert failed.stored_at is None
