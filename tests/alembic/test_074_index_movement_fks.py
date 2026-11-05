"""Migration 074: index the foreign keys that point at ``movements.id`` and ``cases.id``.

Deleting a movement (the duplicate-cases merge does it by the tens of thousands)
makes PostgreSQL check every table that references it. Without an index on the
referencing column that check scans the whole table: ``documents.movement_id`` (~250 MB)
and ``alerts.movement_id`` made each DELETE take ~1.4 s and a 25-group batch ~10 min.

As in test_021/test_072, the suite builds its schema from ``Base.metadata``, so the
model-level guard is checked against the metadata and the file tests pin the
revision chain and the statements.
"""

import importlib.util
from pathlib import Path

from app.core.database import Base
import app.models  # noqa: F401  (registers every table on Base.metadata)

_MIGRATION_PATH = (
    Path(__file__).parent.parent.parent / "alembic/versions/074_index_movement_fks.py"
)

# (table, column) that the production database was MISSING an index on (measured
# 2026-10-23 with pg_constraint / pg_index).
EXPECTED = [
    ("alerts", "movement_id"),
    ("documents", "movement_id"),
    ("case_deadlines", "source_movement_id"),
    ("case_deadlines", "verdict_movement_id"),
    ("cartera_snapshots", "case_id"),
]


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_074", _MIGRATION_PATH)
    assert spec is not None, f"Migration file not found: {_MIGRATION_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _leading_indexed(table, column_name):
    """True when the column leads an index, a unique constraint or the primary key."""
    for index in table.indexes:
        if list(index.columns)[0].name == column_name:
            return True
    for constraint in table.constraints:
        cols = list(getattr(constraint, "columns", []))
        if cols and cols[0].name == column_name and constraint.__class__.__name__ in (
            "UniqueConstraint",
            "PrimaryKeyConstraint",
        ):
            return True
    return False


class TestMigration074File:
    def test_revision_chain(self):
        mod = _load_migration()
        assert mod.revision == "074"
        assert mod.down_revision == "073"

    def test_upgrade_and_downgrade_callable(self):
        mod = _load_migration()
        assert callable(mod.upgrade) and callable(mod.downgrade)

    def test_covers_exactly_the_missing_columns(self):
        mod = _load_migration()
        assert sorted((t, c) for _, t, c in mod.INDEXES) == sorted(EXPECTED)

    def test_index_names_fit_postgres_limit_and_are_unique(self):
        mod = _load_migration()
        names = [n for n, _, _ in mod.INDEXES]
        assert len(names) == len(set(names))
        assert all(len(n) <= 63 for n in names)

    def test_idempotent_and_reversible(self):
        """IF NOT EXISTS / IF EXISTS, so a re-run after a partial failure is safe."""
        import inspect

        src = inspect.getsource(_load_migration())
        assert "CREATE INDEX IF NOT EXISTS" in src
        assert "DROP INDEX IF EXISTS" in src


class TestModelDeclaresTheIndexes:
    def test_each_expected_column_is_indexed_in_the_model(self):
        for table_name, column in EXPECTED:
            table = Base.metadata.tables[table_name]
            assert _leading_indexed(table, column), f"{table_name}.{column} has no index"

    def test_every_fk_to_movements_is_indexed(self):
        """Guard for the next table that points at movements.id."""
        missing = []
        for table in Base.metadata.tables.values():
            for fk in table.foreign_keys:
                if fk.column.table.name == "movements" and fk.column.name == "id":
                    if not _leading_indexed(table, fk.parent.name):
                        missing.append(f"{table.name}.{fk.parent.name}")
        assert not missing, f"FKs to movements.id without an index: {missing}"
