"""Migration importability test for 005_add_alert_entity_columns (S2-T01 prereq).

Lightweight sanity-check that the migration file is syntactically valid and
exposes the expected revision identifiers before the real DB apply gate runs.

Note: migration filenames start with digits so they cannot be imported via the
standard dotted path.  We use importlib.util.spec_from_file_location instead.
"""

import importlib.util
from pathlib import Path

import pytest


_MIGRATION_PATH = (
    Path(__file__).parent.parent
    / "alembic/versions/005_add_alert_entity_columns.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_005", _MIGRATION_PATH
    )
    assert spec is not None, f"Migration file not found: {_MIGRATION_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration005Importable:
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
        assert mod.revision == "005"

    def test_down_revision_chains_to_004(self):
        mod = _load_migration()
        assert mod.down_revision == "004"
