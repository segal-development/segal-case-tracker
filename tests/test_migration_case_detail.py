"""Migration importability test for 004_add_case_detail_entities (S1-T09 — RED first).

Verifies that the migration module is importable and exposes upgrade/downgrade
callables — a lightweight sanity check that the file is syntactically valid
before running the real DB apply test in the verification gate.

Note: migration filenames start with digits so they cannot be imported via the
standard dotted path. We use importlib.util.spec_from_file_location instead.
"""

import importlib.util
from pathlib import Path

import pytest


_MIGRATION_PATH = (
    Path(__file__).parent.parent
    / "alembic/versions/004_add_case_detail_entities.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_004", _MIGRATION_PATH
    )
    assert spec is not None, f"Migration file not found: {_MIGRATION_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration004Importable:
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
        assert mod.revision == "004"

    def test_down_revision_chains_to_003(self):
        mod = _load_migration()
        assert mod.down_revision == "003"
