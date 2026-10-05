"""Migration 063: verdict columns on case_deadlines.

Runs upgrade()/downgrade() for real (SQLite) against a table that already has
rows, to prove the change is additive: existing rows survive untouched with a
NULL verdict ("sin determinar") and no default rewrites them.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

_REPO_ROOT = Path(__file__).parent.parent.parent
_PATH = _REPO_ROOT / "alembic/versions/063_case_deadline_verdict.py"
_NEW = {"verdict", "verdict_movement_id", "verdict_acted_on", "verdict_computed_at"}


def _load():
    spec = importlib.util.spec_from_file_location("migration_063", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _engine_with_rows():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE movements (id INTEGER PRIMARY KEY)"))
        conn.execute(sa.text(
            "CREATE TABLE case_deadlines (id INTEGER PRIMARY KEY, status VARCHAR(20) NOT NULL)"
        ))
        conn.execute(sa.text(
            "INSERT INTO case_deadlines (id, status) VALUES (1, 'superseded'), (2, 'active')"
        ))
    return engine


def _columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("case_deadlines")}


class _SqliteOp:
    """Delegates to alembic ``op`` but strips FOREIGN KEYs from add_column.

    SQLite cannot ALTER ... ADD CONSTRAINT, and production is Postgres. The FK
    declaration is asserted here (target + ON DELETE SET NULL) before it is
    stripped; the rest of the DDL runs for real. The Postgres FK DDL itself is
    NOT exercised by this suite.
    """

    def __init__(self, real_op, seen_fks):
        self._op, self._seen = real_op, seen_fks

    def add_column(self, table, column):
        for fk in column.foreign_keys:
            self._seen[column.name] = (fk.target_fullname, fk.ondelete)
        plain = sa.Column(column.name, column.type, nullable=column.nullable)
        return self._op.add_column(table, plain)

    def __getattr__(self, name):
        return getattr(self._op, name)


def _run(conn, fn) -> None:
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        fn()


def _sqlite_migration():
    mod = _load()
    seen: dict = {}
    mod.op = _SqliteOp(mod.op, seen)
    return mod, seen


def test_chain_links_to_062():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("063", "062")


def test_upgrade_adds_nullable_columns_and_keeps_existing_rows_untouched():
    engine = _engine_with_rows()
    with engine.begin() as conn:
        _run(conn, _sqlite_migration()[0].upgrade)
        assert _NEW <= _columns(conn)
        rows = conn.execute(sa.text(
            "SELECT status, verdict, verdict_movement_id, verdict_acted_on, "
            "verdict_computed_at FROM case_deadlines ORDER BY id"
        )).all()
    assert rows == [
        ("superseded", None, None, None, None),
        ("active", None, None, None, None),
    ]


def test_upgrade_columns_have_no_default():
    """A default would force a table rewrite on Postgres for the existing rows."""
    engine = _engine_with_rows()
    with engine.begin() as conn:
        _run(conn, _sqlite_migration()[0].upgrade)
        cols = {c["name"]: c for c in sa.inspect(conn).get_columns("case_deadlines")}
    for name in _NEW:
        assert cols[name]["nullable"] is True
        assert cols[name]["default"] is None


def test_downgrade_removes_the_columns_and_keeps_the_rows():
    engine = _engine_with_rows()
    mod, _ = _sqlite_migration()
    with engine.begin() as conn:
        _run(conn, mod.upgrade)
        _run(conn, mod.downgrade)
        assert not (_NEW & _columns(conn))
        assert conn.execute(sa.text("SELECT count(*) FROM case_deadlines")).scalar() == 2


def test_movement_reference_is_a_fk_that_nulls_on_delete():
    """Case merge deletes duplicate movements: the verdict must not block it."""
    engine = _engine_with_rows()
    mod, seen = _sqlite_migration()
    with engine.begin() as conn:
        _run(conn, mod.upgrade)
    assert seen == {"verdict_movement_id": ("movements.id", "SET NULL")}
