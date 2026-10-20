"""Tests for migration 071: backfill Case.last_movement_at.

File-sanity and chaining checks, matching the pattern of the other migration
tests here (the suite builds its schema from ``Base.metadata``, not by running
Alembic, so the SQL is not exercised against a live DB).

Beyond shape, these pin the two properties that make this data migration safe
to re-run and safe to deploy: it only ever moves a row FORWARD, and it only
touches cases that actually have movements.
"""

import importlib.util
import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_REPO_ROOT = Path(__file__).parent.parent.parent
_MIGRATION_PATH = _REPO_ROOT / "alembic/versions/071_backfill_last_movement_at.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_071", _MIGRATION_PATH)
    assert spec is not None, f"Migration file not found: {_MIGRATION_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sql() -> str:
    return _MIGRATION_PATH.read_text()


class TestMigration071Shape:
    def test_migration_file_exists(self) -> None:
        assert _MIGRATION_PATH.exists(), f"Expected migration at {_MIGRATION_PATH}"

    def test_upgrade_and_downgrade_are_callable(self) -> None:
        mod = _load_migration()
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)

    def test_it_chains_onto_070(self) -> None:
        mod = _load_migration()
        assert mod.revision == "071"
        assert mod.down_revision == "070"

    def test_the_alembic_chain_still_has_a_single_head(self) -> None:
        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        heads = list(script.get_heads())
        assert len(heads) == 1, f"Alembic chain forked: {heads}"


class TestMigration071IsSafe:
    def test_it_only_ever_moves_a_row_forward(self) -> None:
        """Without this guard a re-run, or a case whose column is already
        ahead, would have its value pulled backwards."""
        sql = _sql()
        assert re.search(
            r"c\.last_movement_at\s+IS\s+NULL\s+OR\s+c\.last_movement_at\s*<\s*m\.newest",
            sql,
            re.IGNORECASE,
        ), "the UPDATE must be guarded so it only moves last_movement_at forward"

    def test_it_only_touches_cases_that_have_movements(self) -> None:
        """The join is against an aggregate over ``movements``, so a case with
        no rows is never reached and keeps whatever it had. A sync never
        deletes movements, so "no rows" means "we were told nothing", not
        "nothing happened"."""
        sql = _sql()
        assert "FROM movements" in sql
        assert re.search(r"m\.case_id\s*=\s*c\.id", sql, re.IGNORECASE)
        assert re.search(r"movement_date\s+IS\s+NOT\s+NULL", sql, re.IGNORECASE)
