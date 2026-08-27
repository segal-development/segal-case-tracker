"""Tests for migration 052: performance indexes.

Lightweight file-sanity checks matching the pattern used by other migration
tests in this repo, plus an assertion that 052 is the single Alembic head so
the linear migration chain is not accidentally forked.

The indexes themselves are not exercised against a live DB — the test suite
builds its schema from ``Base.metadata`` (see tests/conftest.py), not by
running Alembic — so this only verifies the migration's shape and chaining.
"""

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_REPO_ROOT = Path(__file__).parent.parent.parent
_MIGRATION_PATH = _REPO_ROOT / "alembic/versions/052_perf_indexes.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_052", _MIGRATION_PATH)
    assert spec is not None, f"Migration file not found: {_MIGRATION_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration052Importable:
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
        assert mod.revision == "052"

    def test_down_revision_chains_to_051(self):
        mod = _load_migration()
        assert mod.down_revision == "051"


class TestMigrationChainHasSingleHead:
    def test_052_is_the_single_head(self):
        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        heads = list(script.get_heads())
        assert heads == ["052"], f"Expected single head 052, got {heads}"
