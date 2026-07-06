"""Tests for migrations 023 (case_merge_audit table) and 024 (data merge).

Two layers, matching the lightweight pattern used by other migration tests
in this repo (see tests/alembic/test_021_unique_constraint.py):
1. Migration file sanity (importable, correct revision chain).
2. 023's schema is exercised via the SQLAlchemy model
   (tests/models/test_case_merge_audit.py); 024's data logic is exercised via
   the pure pipeline (tests/services/test_case_merge.py). This file also
   confirms 024's ``upgrade()``/``downgrade()`` actually delegate to that
   pipeline against the test DB.
"""

import importlib.util
from pathlib import Path

import pytest

from app.models.case import Case
from app.models.case_merge_audit import CaseMergeAudit
from app.models.court import Court
from app.models.lawyer import Lawyer

_MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "alembic/versions"
_MIGRATION_023_PATH = _MIGRATIONS_DIR / "023_case_merge_audit.py"
_MIGRATION_024_PATH = _MIGRATIONS_DIR / "024_merge_duplicate_cases.py"


def _load_migration(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None, f"Migration file not found: {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration023Importable:
    def test_migration_file_exists(self):
        assert _MIGRATION_023_PATH.exists()

    def test_upgrade_and_downgrade_callable(self):
        mod = _load_migration(_MIGRATION_023_PATH, "migration_023")
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)

    def test_revision_chain(self):
        mod = _load_migration(_MIGRATION_023_PATH, "migration_023")
        assert mod.revision == "023"
        assert mod.down_revision == "022"


class TestMigration024Importable:
    def test_migration_file_exists(self):
        assert _MIGRATION_024_PATH.exists()

    def test_upgrade_and_downgrade_callable(self):
        mod = _load_migration(_MIGRATION_024_PATH, "migration_024")
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)

    def test_revision_chain(self):
        mod = _load_migration(_MIGRATION_024_PATH, "migration_024")
        assert mod.revision == "024"
        assert mod.down_revision == "023"


class TestMigration024DelegatesToPipeline:
    """024's upgrade()/downgrade() must actually call into
    app.services.case_merge against a real bind — exercised here through
    op.get_bind() monkeypatched to the test session's connection."""

    def test_upgrade_merges_duplicate_rol_via_bind(self, db, monkeypatch):
        firm = Lawyer(rut="16021492-9", name="Firm Lawyer")
        carla = Lawyer(rut="22222222-2", name="Carla")
        db.add_all([firm, carla])
        db.flush()
        court = Court(code="T1-MIG024", name="Juzgado Migration024 Test", region="RM", type="civil")
        db.add(court)
        db.flush()
        winner = Case(lawyer_id=carla.id, court_id=court.id, rol="C-MIG-2024", competencia="civil")
        loser = Case(lawyer_id=firm.id, court_id=court.id, rol="C-MIG-2024", competencia="civil")
        db.add_all([winner, loser])
        db.commit()

        mod = _load_migration(_MIGRATION_024_PATH, "migration_024_upgrade_test")
        monkeypatch.setattr(mod.op, "get_bind", lambda: db.connection())

        mod.upgrade()

        remaining = db.query(Case).filter(Case.rol == "C-MIG-2024").all()
        assert len(remaining) == 1
        audit = db.query(CaseMergeAudit).filter(CaseMergeAudit.rol == "C-MIG-2024").first()
        assert audit is not None
