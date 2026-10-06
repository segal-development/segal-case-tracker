"""Migration 064: ``origin`` column on case_deadlines (additive, nullable)."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

_PATH = Path(__file__).parent.parent.parent / "alembic/versions/064_case_deadline_origin.py"


def _load():
    spec = importlib.util.spec_from_file_location("migration_064", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _engine():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE case_deadlines (id INTEGER PRIMARY KEY, status VARCHAR(20))"))
        conn.execute(sa.text("INSERT INTO case_deadlines (id, status) VALUES (1, 'active')"))
    return engine


def _run(conn, mod, fn):
    with Operations.context(MigrationContext.configure(conn)):
        getattr(mod, fn)()


def _cols(conn):
    return {c["name"] for c in sa.inspect(conn).get_columns("case_deadlines")}


def test_revision_chain():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("064", "063")


def test_upgrade_adds_nullable_origin_and_keeps_rows():
    mod, engine = _load(), _engine()
    with engine.begin() as conn:
        _run(conn, mod, "upgrade")
        assert "origin" in _cols(conn)
        assert conn.execute(sa.text("SELECT origin FROM case_deadlines WHERE id = 1")).scalar() is None


def test_downgrade_removes_origin():
    mod, engine = _load(), _engine()
    with engine.begin() as conn:
        _run(conn, mod, "upgrade")
        _run(conn, mod, "downgrade")
        assert "origin" not in _cols(conn)
