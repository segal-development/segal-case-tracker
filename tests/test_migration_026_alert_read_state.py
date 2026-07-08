"""Migration importability test for 026_add_alert_read_state.

Lightweight sanity-check that the migration file is syntactically valid and
exposes the expected revision identifiers before the real DB apply gate runs.
Matches the pattern used by every other additive-column migration in this
repo (see test_migration_025_credential_alert_sent_at.py).

Note: migration filenames start with digits so they cannot be imported via
the standard dotted path. We use importlib.util.spec_from_file_location
instead.
"""

import importlib.util
from pathlib import Path


_MIGRATION_PATH = (
    Path(__file__).parent.parent
    / "alembic/versions/026_add_alert_read_state.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_026", _MIGRATION_PATH
    )
    assert spec is not None, f"Migration file not found: {_MIGRATION_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration026Importable:
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
        assert mod.revision == "026"

    def test_down_revision_chains_to_025(self):
        mod = _load_migration()
        assert mod.down_revision == "025"
